"""统一 AI 文案配置中心：复用 AI 助手多渠道底座、支持人设预设与代理策略。"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .agent.config import (
    AgentSettings,
    ConnectionSettings,
    SettingsUpdate as AgentSettingsUpdate,
    get_settings as get_agent_channel_settings,
    list_channels as list_agent_channels,
    save_channel as save_agent_channel,
)
from .db import get_session
from .models import AppSetting
from .settings import get_setting, set_setting

COPYWRITING_SETTINGS_KEY = "ai.copywriting.v1"
_config_lock = threading.RLock()

PLATFORM_LABELS: dict[str, str] = {
    "douyin": "抖音",
    "xhs": "小红书",
    "rednote": "海外小红书",
    "kuaishou": "快手",
    "shipinhao": "微信视频号",
    "tiktok": "TikTok",
    "bilibili": "哔哩哔哩",
    "instagram": "Instagram",
    "twitter": "X (Twitter)",
    "facebook": "Facebook",
    "youtube": "YouTube",
}
OVERSEAS_PLATFORMS = {"instagram", "twitter", "facebook", "youtube", "tiktok", "rednote"}


class PromptPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=200)
    style_prompt: str = Field(default="", max_length=2000)
    max_chars: int = Field(default=60, ge=15, le=300)
    temperature: float | None = Field(default=0.85, ge=0.0, le=2.0)
    language_mode: Literal["auto", "zh", "en"] = "auto"
    builtin: bool = False

    @field_validator("name", "description", "style_prompt")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return value.strip()


BUILTIN_PRESETS: tuple[PromptPreset, ...] = (
    PromptPreset(
        id="natural_cn",
        name="自然口语互动",
        description="简短真诚、像真实网友或创作者日常互动，无营销腔和 AI 味。",
        style_prompt=(
            "语气自然真诚、像真人日常随手打字，不要书面语、不要官腔、不要推销感。"
            "紧扣对方提到的细节回应，避免空泛敷衍的万能句。"
        ),
        max_chars=45,
        temperature=0.85,
        language_mode="auto",
        builtin=True,
    ),
    PromptPreset(
        id="friendly_cs",
        name="亲切客服答疑",
        description="适合回复自己作品下的提问、咨询、求攻略类评论，礼貌高效。",
        style_prompt=(
            "以作品作者身份亲切回复粉丝，先回应对方具体问题或夸奖，表达感谢；"
            "若涉及详细教程/同款/合作，可自然引导关注或看主页置顶，不要硬广。"
        ),
        max_chars=75,
        temperature=0.7,
        language_mode="auto",
        builtin=True,
    ),
    PromptPreset(
        id="xhs_seeding",
        name="小红书种草共鸣",
        description="轻松有网感、强调情绪共鸣与体验分享，适配小红书社区氛围。",
        style_prompt=(
            "贴合小红书社区的轻松真实分享风格，多从个人感受或共鸣点切入，"
            "可点缀不超过 1 个自然 emoji，禁止堆砌标签或夸张推销词。"
        ),
        max_chars=55,
        temperature=0.9,
        language_mode="zh",
        builtin=True,
    ),
    PromptPreset(
        id="bili_humor",
        name="B站轻松玩梗",
        description="适合 B 站评论区互动，接地气、简洁带一点轻松幽默。",
        style_prompt=(
            "贴近 B 站评论区接地气、轻松友好的互动感，结合作品或评论内容简短接话，"
            "自然俏皮但不冒犯、不强行说教。"
        ),
        max_chars=55,
        temperature=0.9,
        language_mode="zh",
        builtin=True,
    ),
    PromptPreset(
        id="global_social",
        name="海外社媒自然互动",
        description="适合 Instagram / X / Facebook / YouTube，自动跟随原文语种写出地道简短互动。",
        style_prompt=(
            "Write a concise, genuine, conversational social-media comment or reply. "
            "Match the language of the original post/comment (default to natural English if unclear). "
            "Avoid corporate tone, hashtags, or spammy links."
        ),
        max_chars=120,
        temperature=0.85,
        language_mode="auto",
        builtin=True,
    ),
)

BUILTIN_PRESET_IDS = {p.id for p in BUILTIN_PRESETS}


def _validate_http_url(value: str, *, allow_socks: bool = False, label: str = "地址") -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label}格式无效") from exc
    schemes = {"http", "https", "socks5", "socks5h"} if allow_socks else {"http", "https"}
    if parsed.scheme not in schemes or not parsed.hostname or any(c.isspace() for c in value):
        raise ValueError(f"{label}格式无效，请填写合法的协议与主机地址")
    if not allow_socks and (parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError(f"{label}不应包含账号凭据、查询参数或片段")
    return value


class CopywritingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = False
    source: Literal["channel", "custom"] = "channel"
    # source == "channel" 时：留空表示跟随 AI 助手当前激活渠道 / 渠道默认模型
    channel_id: str = Field(default="", max_length=64)
    model_override: str = Field(default="", max_length=160)
    # source == "custom" 时的独立接口连接参数
    provider: Literal["openai_compatible", "openai_responses", "anthropic"] = "openai_compatible"
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=4096)
    api_path_mode: Literal["auto", "exact"] = "auto"
    model: str = Field(default="", max_length=160)
    output_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    # 通用生成参数与网络配置
    temperature: float | None = Field(default=0.85, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=256, ge=64, le=2048)
    timeout_seconds: int = Field(default=25, ge=5, le=90)
    max_chars: int = Field(default=60, ge=15, le=300)
    language_mode: Literal["auto", "zh", "en"] = "auto"
    disable_thinking: bool = True
    proxy_mode: Literal["direct", "system", "custom"] = "direct"
    proxy_url: str = Field(default="", max_length=500)
    # 提示词与人设预设
    default_preset_id: str = Field(default="natural_cn", max_length=64)
    custom_prompt: str = Field(default="", max_length=4000)
    custom_presets: list[PromptPreset] = Field(default_factory=list, max_length=20)

    @field_validator("channel_id", "model_override", "model", "default_preset_id")
    @classmethod
    def clean_ident(cls, value: str) -> str:
        value = (value or "").strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("配置标识包含非法控制字符")
        return value

    @field_validator("api_key")
    @classmethod
    def clean_key(cls, value: str) -> str:
        value = (value or "").strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("API Key 包含非法控制字符，请重新粘贴")
        return value

    @field_validator("base_url")
    @classmethod
    def clean_base_url(cls, value: str) -> str:
        return _validate_http_url(value, allow_socks=False, label="模型接口地址")

    @field_validator("proxy_url")
    @classmethod
    def clean_proxy_url(cls, value: str) -> str:
        return _validate_http_url(value, allow_socks=True, label="代理地址")

    @field_validator("custom_presets")
    @classmethod
    def validate_custom_presets(cls, items: list[PromptPreset]) -> list[PromptPreset]:
        seen: set[str] = set()
        cleaned: list[PromptPreset] = []
        for item in items:
            if item.id in BUILTIN_PRESET_IDS:
                raise ValueError(f"自定义人设 ID 不能与内置预设冲突: {item.id}")
            if item.id in seen:
                raise ValueError(f"自定义人设 ID 重复: {item.id}")
            seen.add(item.id)
            cleaned.append(item.model_copy(update={"builtin": False}))
        return cleaned

    @model_validator(mode="after")
    def validate_proxy_and_preset(self):
        if self.proxy_mode == "custom" and not self.proxy_url:
            raise ValueError("选择自定义代理时，请填写代理地址（如 http://127.0.0.1:7890）")
        valid_ids = BUILTIN_PRESET_IDS | {p.id for p in self.custom_presets}
        if self.default_preset_id not in valid_ids:
            self.default_preset_id = "natural_cn"
        return self


class CopywritingConfigUpdate(CopywritingConfig):
    clear_api_key: bool = False


def _parse_legacy_temp(raw: str) -> float | None:
    s = (raw or "").strip()
    if not s:
        return 0.85
    try:
        v = float(s)
        if 0.0 <= v <= 2.0:
            return round(v, 2)
    except Exception:
        pass
    return 0.85


def _load_legacy_config() -> CopywritingConfig:
    """兼容升级：无 ai.copywriting.v1 时自动从旧版 ai_* 键值读取。"""
    legacy_enabled = get_setting("ai_enabled", "0") == "1"
    legacy_base = get_setting("ai_base_url", "").strip()
    legacy_key = get_setting("ai_api_key", "").strip()
    legacy_model = get_setting("ai_model", "").strip()
    legacy_prompt = get_setting("ai_prompt", "")
    legacy_temp = _parse_legacy_temp(get_setting("ai_temperature", "0.9"))
    has_legacy_custom = bool(legacy_base or legacy_model or legacy_key)
    if has_legacy_custom:
        safe_base = ""
        try:
            safe_base = _validate_http_url(legacy_base, allow_socks=False, label="模型接口地址")
        except ValueError:
            safe_base = ""
        return CopywritingConfig(
            enabled=legacy_enabled,
            source="custom",
            provider="openai_compatible",
            base_url=safe_base,
            api_key=legacy_key,
            api_path_mode="auto",
            model=legacy_model,
            temperature=legacy_temp,
            custom_prompt=legacy_prompt,
        )
    return CopywritingConfig(
        enabled=legacy_enabled,
        source="channel",
        temperature=legacy_temp,
        custom_prompt=legacy_prompt,
    )


def get_copywriting_config() -> CopywritingConfig:
    with get_session() as session:
        row = session.get(AppSetting, COPYWRITING_SETTINGS_KEY)
        if row and row.value:
            try:
                return CopywritingConfig.model_validate_json(row.value)
            except Exception:
                pass
    return _load_legacy_config()


def all_presets(cfg: CopywritingConfig | None = None) -> list[PromptPreset]:
    cfg = cfg or get_copywriting_config()
    return [*BUILTIN_PRESETS, *cfg.custom_presets]


def find_preset(preset_id: str | None, cfg: CopywritingConfig | None = None) -> PromptPreset:
    cfg = cfg or get_copywriting_config()
    target = (preset_id or "").strip() or cfg.default_preset_id
    for item in all_presets(cfg):
        if item.id == target:
            return item
    return BUILTIN_PRESETS[0]


def _resolve_custom_key(body: CopywritingConfigUpdate, old: CopywritingConfig) -> str:
    if body.clear_api_key:
        return ""
    if body.api_key:
        return body.api_key
    if body.source == "custom" and old.api_key and (body.base_url, body.provider) != (old.base_url, old.provider):
        raise ValueError("更换自定义接口地址或协议时，请重新填写 API Key，或勾选清除已保存密钥")
    return old.api_key


def save_copywriting_config(body: CopywritingConfigUpdate) -> CopywritingConfig:
    with _config_lock:
        old = get_copywriting_config()
        values = body.model_dump(exclude={"clear_api_key"})
        values["api_key"] = _resolve_custom_key(body, old)
        cfg = CopywritingConfig(**values)

        if cfg.source == "channel" and cfg.channel_id:
            try:
                get_agent_channel_settings(cfg.channel_id)
            except ValueError as exc:
                raise ValueError("所选共享模型渠道不存在，请刷新渠道列表后重选") from exc

        if cfg.enabled:
            if cfg.source == "custom":
                if not (cfg.base_url and cfg.model):
                    raise ValueError("启用独立接口前，请填写接口地址和模型名称")
            else:
                try:
                    ch = get_agent_channel_settings(cfg.channel_id or None)
                except ValueError as exc:
                    raise ValueError("所选共享模型渠道不可用，请先在 AI 助手中配置模型渠道") from exc
                eff_model = cfg.model_override or ch.model
                if not (ch.base_url and eff_model):
                    raise ValueError("所选共享模型渠道尚未配置接口地址或模型，请先在 AI 助手的「模型设置」中补全并保存")

        with get_session() as session:
            row = session.get(AppSetting, COPYWRITING_SETTINGS_KEY) or AppSetting(key=COPYWRITING_SETTINGS_KEY)
            row.value = json.dumps(cfg.model_dump(), ensure_ascii=False)
            session.add(row)
            session.commit()

        # 同步旧版扁平键值，保证历史读取方语义一致
        set_setting("ai_enabled", "1" if cfg.enabled else "0")
        set_setting("ai_prompt", cfg.custom_prompt)
        set_setting("ai_temperature", "" if cfg.temperature is None else str(cfg.temperature))
        if cfg.source == "custom":
            set_setting("ai_base_url", cfg.base_url)
            set_setting("ai_model", cfg.model)
            set_setting("ai_api_key", cfg.api_key)
        elif body.clear_api_key:
            set_setting("ai_api_key", "")
        return cfg


def migrate_custom_to_shared_channel(display_name: str = "AI 文案渠道") -> dict[str, Any]:
    """把独立自定义 AI 文案配置一键沉淀为 AI 助手共享模型渠道，并切换为渠道复用模式。"""
    with _config_lock:
        cfg = get_copywriting_config()
        if cfg.source != "custom" or not (cfg.base_url and cfg.model):
            raise ValueError("当前没有可迁移的独立接口配置（需已保存接口地址与模型名称）")
        clean_name = re.sub(r"[\x00-\x1f\x7f]+", "", (display_name or "").strip())[:80] or "AI 文案渠道"
        payload = AgentSettingsUpdate(
            display_name=clean_name,
            provider=cfg.provider,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            api_path_mode=cfg.api_path_mode,
            model=cfg.model,
            models=[{"id": cfg.model, "name": cfg.model}],
            enabled=False,
            output_token_parameter=cfg.output_token_parameter,
            timeout_seconds=max(5, min(90, cfg.timeout_seconds)),
            max_output_tokens=max(256, min(8192, cfg.max_output_tokens)),
        )
        created = save_agent_channel(payload, create=True)
        new_id = str((created.get("channel") or {}).get("id") or "")
        if not new_id:
            raise ValueError("创建共享渠道失败")
        updated = CopywritingConfigUpdate(
            **{
                **cfg.model_dump(exclude={"api_key"}),
                "source": "channel",
                "channel_id": new_id,
                "model_override": "",
                "api_key": "",
                "clear_api_key": True,
            }
        )
        saved = save_copywriting_config(updated)
        return {"channel": created.get("channel"), "ai_config": public_copywriting_config(saved)}


def _channel_summary(raw_channels: dict[str, Any]) -> dict[str, Any]:
    active_id = str(raw_channels.get("active_channel_id") or "default")
    items = []
    for ch in raw_channels.get("channels") or []:
        cid = str(ch.get("id") or "")
        name = (ch.get("display_name") or "").strip() or ("默认渠道" if cid == "default" else "未命名渠道")
        models = ch.get("models") or []
        cur_model = (ch.get("model") or "").strip()
        model_list = [{"id": str(m.get("id") or ""), "name": str(m.get("name") or m.get("id") or "")}
                      for m in models if isinstance(m, dict) and m.get("id")]
        if cur_model and not any(m["id"] == cur_model for m in model_list):
            model_list.insert(0, {"id": cur_model, "name": cur_model})
        items.append({
            "id": cid,
            "display_name": name,
            "provider": ch.get("provider") or "openai_compatible",
            "base_url": ch.get("base_url") or "",
            "model": cur_model,
            "models": model_list,
            "enabled": bool(ch.get("enabled")),
            "api_key_set": bool(ch.get("api_key_set")),
            "configured": bool(ch.get("base_url") and cur_model),
            "is_active": cid == active_id,
        })
    return {"active_channel_id": active_id, "channels": items}


def get_shared_channels_summary() -> dict[str, Any]:
    return _channel_summary(list_agent_channels())


def public_copywriting_config(cfg: CopywritingConfig | None = None) -> dict[str, Any]:
    cfg = cfg or get_copywriting_config()
    channels_info = get_shared_channels_summary()
    resolved_preview = describe_effective_route(cfg, channels_info=channels_info)
    data = cfg.model_dump(exclude={"api_key"})
    data["api_key_set"] = bool(cfg.api_key)
    data["presets"] = [p.model_dump() for p in all_presets(cfg)]
    data["resolved"] = resolved_preview
    return data


def describe_effective_route(
    cfg: CopywritingConfig | None = None,
    *,
    rule_channel_id: str = "",
    rule_model: str = "",
    channels_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回面向 UI 展示的脱敏路由摘要（绝不含 API Key）。"""
    cfg = cfg or get_copywriting_config()
    channels_info = channels_info or get_shared_channels_summary()
    channels = channels_info.get("channels") or []
    active_id = channels_info.get("active_channel_id") or "default"

    use_channel = bool(rule_channel_id) or cfg.source == "channel"
    if use_channel:
        chosen_id = rule_channel_id or cfg.channel_id or active_id
        ch = next((c for c in channels if c["id"] == chosen_id), None)
        if not ch:
            return {
                "ready": False,
                "source": "channel",
                "label": "所选共享渠道不存在",
                "provider": "",
                "model": "",
                "channel_id": chosen_id,
                "channel_name": "",
                "reason": "所选共享渠道不存在，请重新选择",
            }
        model = (rule_model or cfg.model_override or ch.get("model") or "").strip()
        ready = bool(ch.get("base_url") and model)
        return {
            "ready": ready,
            "source": "channel",
            "label": f'{ch["display_name"]} · {model or "未选模型"}',
            "provider": ch.get("provider") or "openai_compatible",
            "model": model,
            "channel_id": ch["id"],
            "channel_name": ch["display_name"],
            "following_active": not (rule_channel_id or cfg.channel_id),
            "reason": "" if ready else f'共享渠道「{ch["display_name"]}」尚未填写模型地址或模型 ID',
        }

    model = (rule_model or cfg.model or "").strip()
    ready = bool(cfg.base_url and model)
    return {
        "ready": ready,
        "source": "custom",
        "label": f'独立接口 · {model or "未填模型"}',
        "provider": cfg.provider,
        "model": model,
        "channel_id": "",
        "channel_name": "独立自定义接口",
        "following_active": False,
        "reason": "" if ready else "独立接口尚未填写接口地址或模型名称",
    }


def resolve_runtime_ai(
    *,
    draft: CopywritingConfigUpdate | None = None,
    rule_override: dict[str, Any] | None = None,
    require_enabled: bool = True,
) -> dict[str, Any] | None:
    """解析一次实际推理所需的完整配置字典（含密钥与最终生效人设/参数）。

    当 require_enabled=True 且全局总开关关闭时返回 None。
    """
    saved = get_copywriting_config()
    if draft is not None:
        values = draft.model_dump(exclude={"clear_api_key"})
        values["api_key"] = _resolve_custom_key(draft, saved)
        cfg = CopywritingConfig(**values)
    else:
        cfg = saved

    if require_enabled and not cfg.enabled:
        return None

    ov = rule_override or {}
    rule_channel_id = str(ov.get("ai_channel_id") or "").strip()
    rule_model = str(ov.get("ai_model") or "").strip()
    rule_preset_id = str(ov.get("ai_preset_id") or "").strip()
    rule_prompt = str(ov.get("ai_prompt") or "").strip()
    rule_max_chars = int(ov.get("ai_max_chars") or 0)

    preset = find_preset(rule_preset_id or cfg.default_preset_id, cfg)

    use_channel = bool(rule_channel_id) or cfg.source == "channel"
    if use_channel:
        chosen_channel_id = rule_channel_id or cfg.channel_id or None
        ch_settings: AgentSettings = get_agent_channel_settings(chosen_channel_id)
        provider = ch_settings.provider
        base_url = ch_settings.base_url
        api_key = ch_settings.api_key
        api_path_mode = ch_settings.api_path_mode
        model = rule_model or cfg.model_override or ch_settings.model
        output_token_parameter = ch_settings.output_token_parameter
        source_label = ch_settings.display_name or (chosen_channel_id or "默认渠道")
    else:
        provider = cfg.provider
        base_url = cfg.base_url
        api_key = cfg.api_key
        api_path_mode = cfg.api_path_mode
        model = rule_model or cfg.model
        output_token_parameter = cfg.output_token_parameter
        source_label = "独立接口"

    max_chars = rule_max_chars if rule_max_chars > 0 else (cfg.max_chars or preset.max_chars)
    max_chars = max(15, min(300, int(max_chars)))
    temperature = cfg.temperature if cfg.temperature is not None else preset.temperature
    language_mode = cfg.language_mode if cfg.language_mode != "auto" else preset.language_mode

    return {
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "api_path_mode": api_path_mode,
        "model": model,
        "output_token_parameter": output_token_parameter,
        "temperature": temperature,
        "max_output_tokens": cfg.max_output_tokens,
        "timeout": cfg.timeout_seconds,
        "max_chars": max_chars,
        "language_mode": language_mode,
        "disable_thinking": cfg.disable_thinking,
        "proxy_mode": cfg.proxy_mode,
        "proxy_url": cfg.proxy_url,
        "preset_id": preset.id,
        "preset_name": preset.name,
        "style_prompt": preset.style_prompt,
        "prompt": cfg.custom_prompt,
        "rule_prompt": rule_prompt,
        "source_label": source_label,
    }


def make_connection(ai: dict[str, Any]) -> ConnectionSettings:
    return ConnectionSettings(
        provider=ai.get("provider") or "openai_compatible",
        base_url=ai.get("base_url") or "",
        api_key=ai.get("api_key") or "",
        api_path_mode=ai.get("api_path_mode") or "auto",
    )
