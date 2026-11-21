"""全局下载/AI/采集引擎配置读写、通道传输矩阵与 AI 连通性校验接口。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ValidationError
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.engine_settings import EngineSettingsPatch, export_engine_settings, save_engine_settings
from app.models import DouyinAccount
from app.services import metadata as metadata_service
from app.settings import get_setting, set_setting
from app.transport_matrix import build_transport_matrix

router = APIRouter()


class SettingsIn(BaseModel):
    download_dir: str | None = None
    video_quality: str | None = None
    # 大模型 API 文案生成(自动评论用;OpenAI 兼容接口)
    ai_enabled: bool | None = None
    ai_base_url: str | None = None
    ai_api_key: str | None = None        # 留空=不改(避免误清空已存的 key)
    ai_model: str | None = None
    ai_prompt: str | None = None
    ai_temperature: str | None = None


def _settings_dict() -> dict:
    return {
        "download_dir": get_setting("download_dir", app_runtime.cfg.engine.media_dir),
        "video_quality": get_setting("video_quality", "highest"),
        "ai_enabled": get_setting("ai_enabled", "0") == "1",
        "ai_base_url": get_setting("ai_base_url", ""),
        "ai_model": get_setting("ai_model", ""),
        "ai_prompt": get_setting("ai_prompt", ""),
        "ai_temperature": get_setting("ai_temperature", "0.9"),
        # 不回传明文 key,只告知是否已配置
        "ai_api_key_set": bool(get_setting("ai_api_key", "")),
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
    # BrowserManager snapshots native write-gate values at construction. Keep
    # those hot-reloadable switches in sync when they are edited from the UI;
    # other browser executable/profile settings intentionally remain restart-only.
    if app_runtime.browser is not None:
        for key in (
                "native_write_gate_enabled",
                "native_write_require_system_chrome",
                "native_write_require_verified_proxy",
                "native_write_proxy_max_age_seconds"):
            if key in body and hasattr(app_runtime.browser, key):
                setattr(app_runtime.browser, key, getattr(app_runtime.cfg.engine, key))
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
    if body.ai_enabled is not None:
        set_setting("ai_enabled", "1" if body.ai_enabled else "0")
    if body.ai_base_url is not None:
        set_setting("ai_base_url", body.ai_base_url.strip())
    if body.ai_model is not None:
        set_setting("ai_model", body.ai_model.strip())
    if body.ai_prompt is not None:
        set_setting("ai_prompt", body.ai_prompt)
    if body.ai_temperature is not None:
        set_setting("ai_temperature", (body.ai_temperature or "0.9").strip())
    if body.ai_api_key:    # 仅在传了非空值时更新,留空=保留原 key
        set_setting("ai_api_key", body.ai_api_key.strip())
    return _settings_dict()


class AiTestIn(BaseModel):
    # 可选覆盖(便于保存前先测);留空则用已保存设置。key 留空=用已存的
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    prompt: str | None = None
    temperature: str | None = None


@router.post("/api/settings/ai-test")
async def ai_test(body: AiTestIn):
    """用当前(或传入的)AI 配置做一次最小生成,验证连通性。返回 {ok, sample/error}。"""
    from app.engine import compose
    ai = {
        "base_url": body.base_url if body.base_url is not None else get_setting("ai_base_url", ""),
        "api_key": body.api_key if body.api_key else get_setting("ai_api_key", ""),
        "model": body.model if body.model is not None else get_setting("ai_model", ""),
        "prompt": body.prompt if body.prompt is not None else get_setting("ai_prompt", ""),
        "temperature": body.temperature or get_setting("ai_temperature", "0.9"),
        "timeout": 25,
    }
    if not (ai["base_url"] and ai["api_key"] and ai["model"]):
        raise HTTPException(400, "请先填写 Base URL / 模型,并保存或填入 API Key")
    ctx = {"source_text": "这条视频拍得太治愈了,期待更新!", "nick": "测试用户",
           "kw": "", "platform": "douyin", "mode": "auto_reply"}
    try:
        text = await compose.generate(ctx, ai)
        return {"ok": True, "sample": text}
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        return {"ok": False, "error": f"{msg}(检查 Base URL / Key / 模型 / 网络/代理)"}
