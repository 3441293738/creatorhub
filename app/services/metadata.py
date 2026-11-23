"""跨模块共享的标签元数据、渠道状态、报表时间窗口与序列化工具。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import asyncio
import inspect
import json
import os
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import runtime as app_runtime
from app.db import get_session
from app.engine.share_downloader import clean_platform_share_target
from app.models import CommentTask, CommentWatch, MonitorTarget
from app.platforms.channels import ChannelsProxyManager, get_system_proxy_state
from app.platforms.tiktok.urls import parse_profile_reference, parse_video_reference
from app.profiles import pick_ephemeral_proxy
from app.scheduling import parse_schedule, utc_iso


async def auto_start_persisted_channels_proxy(
    target_cfg: Config,
    proxy_mgr: ChannelsProxyManager,
) -> dict[str, Any]:
    """在 CreatorHub 启动时静默恢复上次开启的内置视频号代理（仅在根证书已受信任时启用系统代理）。"""
    if not bool(getattr(target_cfg.engine, "channels_proxy_enabled", False)):
        return {"started": False, "reason": "disabled"}
    if proxy_mgr.running:
        return {"started": True, "reason": "already_running", "proxy": proxy_mgr.status()}

    data_dir = Path(target_cfg.db_path).resolve().parent
    proxy_mgr.configure_data_dir(data_dir)
    manage_sysproxy = bool(getattr(target_cfg.engine, "channels_proxy_manage_sysproxy", True))
    raw_port = getattr(target_cfg.engine, "channels_proxy_port", None)
    port = 2023 if raw_port is None else int(raw_port)

    if manage_sysproxy:
        ca_status = await asyncio.to_thread(proxy_mgr.ca.trust_status)
        if not ca_status.get("trusted"):
            return {"started": False, "reason": "ca_untrusted"}
        sys_state = await asyncio.to_thread(
            get_system_proxy_state, data_dir=data_dir, backend=proxy_mgr._proxy_backend
        )
        if sys_state.get("low_integrity"):
            return {"started": False, "reason": "low_integrity"}

    state = await proxy_mgr.start(
        proxy_port=port,
        enable_system_proxy_flag=manage_sysproxy,
    )
    return {"started": bool(state.get("running")), "proxy": state}


_QUEUE_FAILED = {"failed", "partial", "uncertain"}


def _queue_iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value else None


def _require_risk_admin(request: Request) -> str:
    expected = os.environ.get("CREATORHUB_ADMIN_TOKEN", "").strip()
    supplied = request.headers.get("X-CreatorHub-Admin-Token", "").strip()
    if expected and not secrets.compare_digest(expected.encode(), supplied.encode()):
        raise HTTPException(403, "需要风控管理口令")
    actor = request.headers.get("X-CreatorHub-Actor", "").strip()[:64]
    if actor:
        return actor
    host = request.client.host if request.client else "local"
    return f"local-ui@{host}"[:64]


class RiskClearIn(BaseModel):
    confirmed: bool = False
    reason: str = ""


class DmActionEditIn(BaseModel):
    content: str


# ─────────── 全局设置 ───────────
QUALITY_CHOICES = {"highest", "1080", "720", "540", "lowest"}


# ─────────── 监控目标 ───────────
def _meta_text(value: str | None, max_len: int) -> str:
    """清理用于界面管理的分组名/别名。"""
    return " ".join((value or "").strip().split())[:max_len]


def _meta_tags(value: list[str] | None) -> list[str] | None:
    """清理、去重标签；None 表示更新时不修改。"""
    if value is None:
        return None
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        tag = " ".join(str(raw or "").strip().split())[:24]
        key = tag.casefold()
        if not tag or key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) >= 12:
            break
    return result


def _load_meta_tags(raw: str | None) -> list[str]:
    """兼容 JSON 与早期手工逗号分隔格式。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return _meta_tags([str(item) for item in data]) or []
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return _meta_tags(str(raw).replace("，", ",").split(",")) or []


def _dump_meta_tags(tags: list[str] | None) -> str:
    return json.dumps(tags or [], ensure_ascii=False, separators=(",", ":"))


def _meta_matches(item: MonitorTarget | CommentWatch, group_name: str, tag: str) -> bool:
    if group_name and item.group_name != group_name:
        return False
    if tag and tag not in _load_meta_tags(item.tags):
        return False
    return True


def _clean_platform_target_input(value: str, platform: str) -> str:
    """复用分享链接下载的清洗器，返回当前平台的第一条有效链接或原始 ID。"""
    target_input, detected_links = clean_platform_share_target(value, platform)
    expected_share_platform = "wechat" if platform == "shipinhao" else platform
    if detected_links and not any(
            item.platform == expected_share_platform for item in detected_links):
        platform_name = {
            "douyin": "抖音", "xhs": "小红书", "rednote": "海外小红书", "kuaishou": "快手",
            "shipinhao": "视频号", "tiktok": "TikTok", "bilibili": "哔哩哔哩",
            "instagram": "Instagram", "twitter": "Twitter",
        }.get(platform, platform)
        detected_names = {
            "douyin": "抖音", "xhs": "小红书", "rednote": "海外小红书", "kuaishou": "快手",
            "wechat": "微信/视频号", "tiktok": "TikTok", "bilibili": "哔哩哔哩",
            "instagram": "Instagram", "twitter": "Twitter",
            "generic": "其他站点",
        }
        names = "、".join(dict.fromkeys(
            detected_names.get(item.platform, item.platform)
            for item in detected_links
        ))
        raise HTTPException(
            400,
            f"分享文案中识别到{names}链接，但当前页面是{platform_name}；"
            f"请粘贴{platform_name}链接或完整分享文案",
        )
    return target_input


def _resolve_ephemeral_proxy(platform: str, account_id: int | None = None) -> str:
    """为短链展开或目标解析获取安全的代理出口，绝不默认走本机直连或系统代理。"""
    try:
        with get_session() as s:
            return pick_ephemeral_proxy(
                s, app_runtime.cfg, platform=platform, account_id=account_id
            )
    except Exception:
        return ""


async def _call_resolver(fn, *args, proxy: str = "", **kwargs):
    """兼容各平台解析函数签名，若支持 proxy 参数则自动注入代理出口。"""
    call_kwargs = dict(kwargs)
    if proxy:
        try:
            params = inspect.signature(fn).parameters
            if "proxy" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            ):
                call_kwargs["proxy"] = proxy
        except Exception:
            pass
    return await fn(*args, **call_kwargs)


async def _resolve_tiktok_target(
    value: str, *, proxy: str = "", account_id: int | None = None
) -> tuple[str, object | None, str]:
    """Resolve TikTok short links via Chrome-impersonated session without system env leaks."""
    target = str(value or "").strip()
    video_ref = parse_video_reference(target)
    unique_id = parse_profile_reference(target)
    if video_ref or unique_id or "://" not in target:
        return target, video_ref, unique_id
    eff_proxy = proxy or _resolve_ephemeral_proxy("tiktok", account_id=account_id)
    try:
        from curl_cffi.requests import AsyncSession
        from app.netfp import curl_proxies

        async with AsyncSession(
            impersonate="chrome136",
            trust_env=False,
            proxies=curl_proxies(eff_proxy),
            timeout=15,
            headers={"User-Agent": app_runtime.cfg.engine.user_agent},
        ) as client:
            response = await client.get(target, allow_redirects=True)
            if getattr(response, "url", None):
                target = str(response.url)
    except Exception:
        pass
    return target, parse_video_reference(target), parse_profile_reference(target)


def _report_window(stmt, model, start: datetime | None, end: datetime | None):
    if start:
        stmt = stmt.where(model.created_at >= start)
    if end:
        stmt = stmt.where(model.created_at < end)
    return stmt


def _report_download(payload: bytes, prefix: str):
    from app.reporting import REPORT_MIME

    filename = f"creatorhub_{prefix}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return StreamingResponse(
        iter([payload]),
        media_type=REPORT_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
        },
    )


def _parse_when(s: str | None, timezone_name: str = "Asia/Shanghai") -> datetime | None:
    try:
        return parse_schedule(s, timezone_name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _task_dict(t: CommentTask) -> dict:
    return {
        "id": t.id, "platform": t.platform, "rule_id": t.rule_id,
        "account_id": t.account_id, "aweme_id": t.aweme_id,
        "target_url": getattr(t, "target_url", ""),
        "target_comment_id": t.target_comment_id, "target_nick": t.target_nick,
        "target_text": getattr(t, "target_text", ""),
        "content": t.content, "status": t.status, "result": t.result,
        "error": t.error, "method": t.method,
        "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
        "next_allowed_at": utc_iso(t.next_allowed_at),
        "blocked_reason": t.blocked_reason,
        "done_at": t.done_at.isoformat() if t.done_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }
