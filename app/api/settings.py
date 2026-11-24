"""全局下载/AI/采集引擎配置读写、通道传输矩阵与 AI 连通性校验接口。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ValidationError
from sqlmodel import select

from app import runtime as app_runtime
from app.ai_copywriting import (
    CopywritingConfigUpdate,
    get_copywriting_config,
    get_shared_channels_summary,
    migrate_custom_to_shared_channel,
    public_copywriting_config,
    resolve_runtime_ai,
    save_copywriting_config,
)
from app.db import get_session
from app.engine_settings import (
    EngineSettingsPatch,
    RESTART_REQUIRED_FIELDS,
    export_engine_settings,
    save_engine_settings,
)
from app.models import DouyinAccount
from app.risk_admin import sync_shared_schedule_to_risk_store
from app.services import metadata as metadata_service
from app.settings import get_setting, set_setting
from app.transport_matrix import build_transport_matrix

router = APIRouter()


class SettingsIn(BaseModel):
    download_dir: str | None = None
    video_quality: str | None = None
    # 新版统一 AI 文案配置（复用 AI 助手共享渠道或独立多协议接口）
    ai_config: CopywritingConfigUpdate | None = None
    # 兼容旧版扁平字段
    ai_enabled: bool | None = None
    ai_base_url: str | None = None
    ai_api_key: str | None = None        # 留空=不改(避免误清空已存的 key)
    ai_model: str | None = None
    ai_prompt: str | None = None
    ai_temperature: str | None = None


def _settings_dict() -> dict:
    ai_cfg = get_copywriting_config()
    ai_public = public_copywriting_config(ai_cfg)
    ai_channels = get_shared_channels_summary()
    return {
        "download_dir": get_setting("download_dir", app_runtime.cfg.engine.media_dir),
        "video_quality": get_setting("video_quality", "highest"),
        # 保留兼容字段
        "ai_enabled": ai_cfg.enabled,
        "ai_base_url": ai_cfg.base_url if ai_cfg.source == "custom" else "",
        "ai_model": ai_public["resolved"].get("model", ""),
        "ai_prompt": ai_cfg.custom_prompt,
        "ai_temperature": "" if ai_cfg.temperature is None else str(ai_cfg.temperature),
        "ai_api_key_set": bool(ai_cfg.api_key),
        # 新版结构化 AI 文案配置与共享渠道列表
        "ai_config": ai_public,
        "ai_channels": ai_channels,
    }


@router.get("/api/settings")
async def get_settings():
    return _settings_dict()


@router.get("/api/settings/engine")
async def get_engine_settings():
    return export_engine_settings(app_runtime.cfg)


@router.get("/api/settings/transport-matrix")
async def get_transport_matrix():
    """Expose actual route support plus account-level isolation diagnostics."""
    with get_session() as session:
        accounts = session.exec(
            select(DouyinAccount).order_by(DouyinAccount.id)).all()
        return build_transport_matrix(app_runtime.cfg, accounts)


@router.put("/api/settings/engine", openapi_extra={
    "requestBody": {"content": {"application/json": {
        "schema": EngineSettingsPatch.model_json_schema()}}}})
async def put_engine_settings(body: Any = Body(...)):
    try:
        result = save_engine_settings(app_runtime.cfg, body)
    except ValidationError as exc:
        # Validate here so rejected NaN/Infinity and accidental secret fields
        # never get echoed in JSON error details (or break JSON serialization).
        details = exc.errors(include_url=False, include_context=False, include_input=False)
        for detail in details:
            detail["loc"] = ["body", *detail["loc"]]
        raise HTTPException(422, details) from exc
    except Exception:
        raise HTTPException(503, "设置尚未保存，请稍后重试；原有配置保持不变") from None
    # Downloader snapshots its timeout at construction. Change the shared
    # instance for future downloads; already-created HTTP clients keep theirs.
    downloader = getattr(app_runtime.engine, "downloader", None)
    if downloader is not None and "download_timeout_seconds" in body:
        downloader.timeout = app_runtime.cfg.engine.download_timeout_seconds
    # BrowserManager snapshots several lifecycle/write-gate values at construction.
    # Keep hot-reloadable fields in sync when edited from the UI.
    if app_runtime.browser is not None and isinstance(body, dict):
        for key in (
                "native_write_gate_enabled",
                "native_write_require_system_chrome",
                "native_write_require_verified_proxy",
                "native_write_proxy_max_age_seconds"):
            if key in body and hasattr(app_runtime.browser, key):
                setattr(app_runtime.browser, key, getattr(app_runtime.cfg.engine, key))
        if "max_live_contexts" in body and hasattr(app_runtime.browser, "max_live"):
            app_runtime.browser.max_live = max(1, int(app_runtime.cfg.engine.max_live_contexts))
        if "resident_browser_sessions" in body and hasattr(app_runtime.browser, "resident_sessions"):
            app_runtime.browser.resident_sessions = bool(app_runtime.cfg.engine.resident_browser_sessions)
        if "browser_session_idle_seconds" in body:
            idle_sec = max(0, int(app_runtime.cfg.engine.browser_session_idle_seconds))
            if hasattr(app_runtime.browser, "session_idle_seconds"):
                app_runtime.browser.session_idle_seconds = idle_sec
            if hasattr(app_runtime.browser, "xhs_cdp_idle_seconds"):
                app_runtime.browser.xhs_cdp_idle_seconds = idle_sec
    if isinstance(body, dict):
        sync_shared_schedule_to_risk_store(body)
        if any(key in RESTART_REQUIRED_FIELDS for key in body):
            result = {**result, "restart_required": True}
    return result


@router.put("/api/settings")
async def put_settings(body: SettingsIn):
    if body.download_dir is not None:
        path = body.download_dir.strip()
        if path:
            try:
                Path(path).expanduser().mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise HTTPException(400, f"目录不可用: {e}")
        set_setting("download_dir", path)
    if body.video_quality is not None:
        q = body.video_quality.strip() or "highest"
        if q not in metadata_service.QUALITY_CHOICES:
            raise HTTPException(400, f"画质取值无效: {q}")
        set_setting("video_quality", q)

    if body.ai_config is not None:
        try:
            save_copywriting_config(body.ai_config)
        except ValidationError as exc:
            details = exc.errors(include_url=False, include_context=False, include_input=False)
            raise HTTPException(422, details) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    elif any(
        val is not None
        for val in (body.ai_enabled, body.ai_base_url, body.ai_api_key, body.ai_model, body.ai_prompt, body.ai_temperature)
    ):
        # 兼容旧版扁平字段更新
        old = get_copywriting_config()
        raw = old.model_dump(exclude={"api_key"})
        raw["api_key"] = body.ai_api_key.strip() if body.ai_api_key else ""
        if body.ai_enabled is not None:
            raw["enabled"] = bool(body.ai_enabled)
        if body.ai_base_url is not None:
            raw["base_url"] = body.ai_base_url.strip()
            if raw["base_url"]:
                raw["source"] = "custom"
        if body.ai_model is not None:
            if raw.get("source") == "custom":
                raw["model"] = body.ai_model.strip()
            else:
                raw["model_override"] = body.ai_model.strip()
        if body.ai_prompt is not None:
            raw["custom_prompt"] = body.ai_prompt
        if body.ai_temperature is not None:
            t = body.ai_temperature.strip()
            if not t:
                raw["temperature"] = None
            else:
                try:
                    raw["temperature"] = float(t)
                except ValueError as exc:
                    raise HTTPException(400, "生成温度需为 0~2 之间的数字") from exc
        try:
            save_copywriting_config(CopywritingConfigUpdate.model_validate(raw))
        except ValidationError as exc:
            details = exc.errors(include_url=False, include_context=False, include_input=False)
            raise HTTPException(422, details) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return _settings_dict()


class AiTestIn(BaseModel):
    # 新版：可直接传入未保存的完整 ai_config 草稿 + 实景测试参数
    ai_config: CopywritingConfigUpdate | None = None
    preset_id: str | None = None
    rule_prompt: str | None = None
    sample_platform: str = "douyin"
    sample_mode: str = "auto_reply"
    sample_work_desc: str = "周末露营实测：轻量化天幕搭建全过程，新手 5 分钟搞定"
    sample_source_text: str = "这条视频拍得太治愈了，请问天幕是什么牌子的呀？期待更新！"
    sample_nick: str = "山风旅人"
    sample_kw: str = ""
    # 兼容旧版扁平参数
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    prompt: str | None = None
    temperature: str | None = None


@router.post("/api/settings/ai-test")
async def ai_test(body: AiTestIn):
    """用当前(或传入的)AI 配置做一次实景生成，验证连通性与生成效果。"""
    import time
    from app.engine import compose

    try:
        if body.ai_config is not None:
            override = {}
            if body.preset_id:
                override["ai_preset_id"] = body.preset_id
            if body.rule_prompt:
                override["ai_prompt"] = body.rule_prompt
            ai = resolve_runtime_ai(draft=body.ai_config, rule_override=override, require_enabled=False)
        elif any(v is not None for v in (body.base_url, body.api_key, body.model, body.prompt, body.temperature)):
            old = get_copywriting_config()
            raw = old.model_dump(exclude={"api_key"})
            raw["api_key"] = body.api_key.strip() if body.api_key else ""
            if body.base_url is not None:
                raw["base_url"] = body.base_url.strip()
                raw["source"] = "custom"
            if body.model is not None:
                if raw.get("source") == "custom":
                    raw["model"] = body.model.strip()
                else:
                    raw["model_override"] = body.model.strip()
            if body.prompt is not None:
                raw["custom_prompt"] = body.prompt
            if body.temperature is not None:
                t = body.temperature.strip()
                raw["temperature"] = float(t) if t else None
            draft = CopywritingConfigUpdate.model_validate(raw)
            override = {"ai_preset_id": body.preset_id or "", "ai_prompt": body.rule_prompt or ""}
            ai = resolve_runtime_ai(draft=draft, rule_override=override, require_enabled=False)
        else:
            override = {"ai_preset_id": body.preset_id or "", "ai_prompt": body.rule_prompt or ""}
            ai = resolve_runtime_ai(rule_override=override, require_enabled=False)
    except ValidationError as exc:
        details = exc.errors(include_url=False, include_context=False, include_input=False)
        raise HTTPException(422, details) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not ai or not (ai.get("base_url") and ai.get("model")):
        raise HTTPException(400, "请先选择有效的模型渠道，或填写接口地址与模型名称")

    mode = body.sample_mode if body.sample_mode in ("auto_reply", "auto_comment") else "auto_reply"
    ctx = {
        "source_text": (body.sample_source_text or "").strip() or "这条视频拍得太治愈了,期待更新!",
        "work_desc": (body.sample_work_desc or "").strip(),
        "nick": (body.sample_nick or "").strip() or "测试用户",
        "kw": (body.sample_kw or "").strip(),
        "platform": (body.sample_platform or "douyin").strip().lower(),
        "mode": mode,
    }
    t0 = time.monotonic()
    try:
        text = await compose.generate(ctx, ai)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return {
            "ok": True,
            "sample": text,
            "elapsed_ms": elapsed_ms,
            "provider": ai.get("provider"),
            "model": ai.get("model"),
            "preset_name": ai.get("preset_name"),
            "source_label": ai.get("source_label"),
        }
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        return {"ok": False, "error": msg, "provider": ai.get("provider"), "model": ai.get("model")}


class AiMigrateIn(BaseModel):
    display_name: str = "AI 文案渠道"


@router.post("/api/settings/ai-migrate-to-channel")
async def ai_migrate_to_channel(body: AiMigrateIn):
    """将当前独立自定义 AI 文案接口一键转存为 AI 助手共享渠道，并切换为渠道复用模式。"""
    try:
        result = migrate_custom_to_shared_channel(body.display_name)
        return {"ok": True, **result, "ai_channels": get_shared_channels_summary()}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
