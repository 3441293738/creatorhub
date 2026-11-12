"""Independent Agent settings. Secrets are write-only at the API boundary."""
from __future__ import annotations

import json
import threading
import uuid
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..db import get_session
from ..models import AppSetting

SETTINGS_KEY = "agent.settings.v1"
CHANNELS_KEY = "agent.channels.v1"
_settings_lock = threading.RLock()


class ConnectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["openai_compatible", "openai_responses", "anthropic"] = "openai_compatible"
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=4096)
    api_path_mode: Literal["auto", "exact"] = "auto"

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("模型地址格式无效") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(char.isspace() for char in value)):
            raise ValueError("模型地址须为 HTTP(S) 基础地址，不含凭据、查询参数或片段")
        return value

    @field_validator("api_key")
    @classmethod
    def clean_key(cls, value: str) -> str:
        value = value.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("API Key 含控制字符，请重新粘贴密钥内容")
        return value


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=160)
    name: str = Field(default="", max_length=200)

    @field_validator("id", "name")
    @classmethod
    def clean_label(cls, value: str, info) -> str:
        value = value.strip()
        if (info.field_name == "id" and not value) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("模型 ID 须非空，模型字段不含控制字符")
        return value


class AgentSettings(ConnectionSettings):
    display_name: str = Field(default="", max_length=80)
    models: list[CatalogModel] = Field(default_factory=list, max_length=1000)
    enabled: bool = False
    mcp_enabled: bool = False
    model: str = Field(default="", max_length=160)
    tool_calling: bool = True
    output_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    timeout_seconds: int = Field(default=45, ge=5, le=90)
    max_rounds: int = Field(default=4, ge=1, le=6)
    max_output_tokens: int = Field(default=2048, ge=256, le=8192)
    reasoning_mode: Literal["default", "summary", "adaptive", "enabled"] = "default"
    thinking_budget: int = Field(default=1024, ge=1024, le=7168)

    @model_validator(mode="after")
    def validate_reasoning(self):
        allowed = {"openai_responses": {"default", "summary"}, "anthropic": {"default", "adaptive", "enabled"},
                   "openai_compatible": {"default", "enabled"}}
        if self.reasoning_mode not in allowed[self.provider]:
            raise ValueError("推理设置与当前协议不匹配，请选择跟随上游默认")
        if self.provider == "anthropic" and self.reasoning_mode == "enabled" and self.thinking_budget >= self.max_output_tokens:
            raise ValueError("思考预算须小于输出上限，请为最终答复预留 token")
        return self

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value: str) -> str:
        value = value.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("显示名称不含控制字符")
        return value

    @field_validator("models")
    @classmethod
    def unique_models(cls, value: list[CatalogModel]) -> list[CatalogModel]:
        if len({item.id for item in value}) != len(value):
            raise ValueError("模型目录中的 ID 不应重复")
        return value

    @field_validator("model")
    @classmethod
    def clean_model(cls, value: str) -> str:
        return value.strip()


class SettingsUpdate(AgentSettings):
    # Omitted/empty keeps the stored key; clearing is always explicit.
    clear_api_key: bool = False


class ModelCatalogRequest(ConnectionSettings):
    # Discovery works before saving/enabling a model and does not persist inputs.
    clear_api_key: bool = False
    # None retains the legacy active-channel behavior; empty means a new channel.
    channel_id: str | None = Field(default=None, max_length=64, pattern=r"^[a-zA-Z0-9_-]*$")


class SavedChannel(AgentSettings):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class ChannelStore(BaseModel):
    active_channel_id: str = "default"
    channels: list[SavedChannel] = Field(min_length=1, max_length=20)


def inference_url(settings: ConnectionSettings) -> str:
    suffix = {"anthropic": "/messages", "openai_responses": "/responses",
              "openai_compatible": "/chat/completions"}[settings.provider]
    base = settings.base_url.rstrip("/")
    if settings.api_path_mode == "auto" and not urlsplit(base).path:
        base += "/v1"
    return base + suffix


def resolve_api_key(body: SettingsUpdate | ModelCatalogRequest, old: AgentSettings) -> str:
    if body.clear_api_key:
        return ""
    if body.api_key:
        return body.api_key
    if (body.base_url, body.provider) != (old.base_url, old.provider) and old.api_key:
        raise ValueError("更换模型服务地址或协议时，请重新填写 API Key，或勾选清除密钥")
    return old.api_key


def _load_channels(session) -> ChannelStore:
    row = session.get(AppSetting, CHANNELS_KEY)
    if row:
        return ChannelStore.model_validate_json(row.value)
    legacy = session.get(AppSetting, SETTINGS_KEY)
    cfg = AgentSettings.model_validate_json(legacy.value) if legacy else AgentSettings()
    # A read never migrates or rewrites the user's old configuration.
    return ChannelStore(channels=[SavedChannel(id="default", **cfg.model_dump())])


def _channel(store: ChannelStore, channel_id: str | None = None) -> SavedChannel:
    chosen = channel_id if channel_id is not None else store.active_channel_id
    for item in store.channels:
        if item.id == chosen:
            return item
    raise ValueError("该模型渠道已不存在，请刷新渠道列表")


def get_settings(channel_id: str | None = None) -> AgentSettings:
    with get_session() as session:
        return AgentSettings(**_channel(_load_channels(session), channel_id).model_dump(exclude={"id"}))


def public_settings(settings: AgentSettings | None = None) -> dict:
    settings = settings or get_settings()
    return {**settings.model_dump(exclude={"api_key"}), "api_key_set": bool(settings.api_key)}


def _public_channels(store: ChannelStore, channel_id: str | None = None) -> dict:
    result = {"active_channel_id": store.active_channel_id,
              "channels": [public_settings(item) for item in store.channels]}
    if channel_id is not None:
        result["channel"] = public_settings(_channel(store, channel_id))
    return result


def list_channels() -> dict:
    with get_session() as session:
        return _public_channels(_load_channels(session))


def _persist_channels(session, store: ChannelStore):
    # Keep the old flat document as an atomic active-channel compatibility view.
    active = _channel(store).model_dump(exclude={"id"})
    for key, value in ((CHANNELS_KEY, store.model_dump()), (SETTINGS_KEY, active)):
        row = session.get(AppSetting, key) or AppSetting(key=key)
        row.value = json.dumps(value, ensure_ascii=False)
        session.add(row)
    session.commit()


def save_channel(body: SettingsUpdate, channel_id: str | None = None, *, create: bool = False) -> dict:
    with _settings_lock, get_session() as session:
        store = _load_channels(session)
        if create:
            if len(store.channels) >= 20:
                raise ValueError("最多保存 20 个模型渠道，请先删除不需要的渠道")
            chosen, old = uuid.uuid4().hex, AgentSettings()
        else:
            old = _channel(store, channel_id)
            chosen = old.id
        values = body.model_dump(exclude={"clear_api_key"})
        values["api_key"] = resolve_api_key(body, old)
        settings = SavedChannel(id=chosen, **values)
        if settings.enabled and not (settings.base_url and settings.model):
            raise ValueError("启用前请填写模型基础地址和模型名称")
        if create:
            store.channels.append(settings)
        else:
            store.channels = [settings if item.id == chosen else item for item in store.channels]
        _persist_channels(session, store)
        return _public_channels(store, chosen)


def save_settings(body: SettingsUpdate) -> dict:
    # Backwards-compatible endpoint edits only the currently active channel.
    result = save_channel(body)["channel"]
    result.pop("id", None)
    return result


def activate_channel(channel_id: str) -> dict:
    with _settings_lock, get_session() as session:
        store = _load_channels(session)
        chosen = _channel(store, channel_id)
        if not chosen.base_url or not chosen.model:
            raise ValueError("该渠道尚未配置模型地址和模型 ID，请先编辑保存")
        store.active_channel_id = chosen.id
        _persist_channels(session, store)
        return _public_channels(store, chosen.id)


def delete_channel(channel_id: str) -> dict:
    with _settings_lock, get_session() as session:
        store = _load_channels(session)
        _channel(store, channel_id)
        if channel_id == store.active_channel_id:
            raise ValueError("请先切换到其他渠道，再删除当前使用的渠道")
        store.channels = [item for item in store.channels if item.id != channel_id]
        _persist_channels(session, store)
        return _public_channels(store)
