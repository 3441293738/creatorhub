"""微信视频号本地协议桥接、系统代理管理、证书安装与桌面微信控制接口。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
import asyncio
import sys

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError

from app import runtime as app_runtime
from app.channels_settings import ChannelsProtocolPatch, export_channels_settings, reset_channels_settings, save_channels_settings, validate_channels_endpoint, validate_channels_endpoint_file
from app.platforms.channels import ChannelsProtocolClient, ChannelsProtocolTransportError, discover_local_cdp_endpoints, discover_wechat_processes, open_or_focus_wechat, restart_wechat
from app.services import collections as collections_service

router = APIRouter()


class ChannelsProbeIn(BaseModel):
    connection_mode: Literal["none", "proxy", "endpoint", "endpoint_file"] | None = None
    cdp_endpoint: str | None = None
    cdp_endpoint_file: str | None = None


class ChannelsBridgeStartIn(BaseModel):
    debug_port: int = 9421
    cdp_port: int = 9422
    allowed_pids: list[int] | None = None
    apply_to_config: bool = True


class ChannelsProxyStartIn(BaseModel):
    proxy_port: int = 2023
    enable_system_proxy: bool = True
    apply_to_config: bool = True
    auto_install_cert: bool = True


class ChannelsProxyStopIn(BaseModel):
    disable_auto_start: bool = False


def _channels_status_payload() -> dict[str, Any]:
    app_runtime.channels_proxy.configure_data_dir(Path(app_runtime.cfg.db_path).resolve().parent)
    config_state = export_channels_settings(app_runtime.cfg)
    read_mode = config_state["read_mode"]
    proxy_configured = bool(config_state.get("proxy_enabled"))
    endpoint_configured = bool(config_state["cdp_endpoint"])
    endpoint_file_configured = bool(config_state["cdp_endpoint_file"])
    configured = bool(config_state["configured"])
    ep_info = config_state.get("endpoint_file_info")
    proxy_state = app_runtime.channels_proxy.status()
    if proxy_configured:
        protocol_usable = bool(proxy_state.get("running"))
    elif endpoint_file_configured and isinstance(ep_info, dict):
        protocol_usable = bool(ep_info.get("exists") and ep_info.get("valid"))
    else:
        protocol_usable = endpoint_configured
    return {
        "platform": "shipinhao",
        "read_mode": read_mode,
        "configured": configured,
        "proxy_configured": proxy_configured,
        "endpoint_configured": endpoint_configured,
        "endpoint_file_configured": endpoint_file_configured,
        "config": config_state,
        "bridge": app_runtime.channels_bridge.status(include_processes=False),
        "proxy": proxy_state,
        "capabilities": {
            "account_works": True,
            "keyword_search": protocol_usable and read_mode != "browser",
            "creator_works": protocol_usable and read_mode != "browser",
            "full_comment_threads": protocol_usable and read_mode != "browser",
            "encrypted_video_decode": True,
            "share_preview": True,
        },
    }


@router.get("/api/channels/protocol/status")
async def channels_protocol_status(probe: bool = False):
    """查询视频号本地微信 Finder 原生协议通道配置与(可选)实时握手状态。"""
    result = _channels_status_payload()
    if probe:
        client = ChannelsProtocolClient.from_config(app_runtime.cfg)
        if client is None:
            result["probe"] = {
                "ok": False,
                "ready": False,
                "error": "尚未配置本地微信通道，请在「设置 → 视频号微信通道」启用代理或配置 CDP 端点",
            }
        else:
            result["probe"] = await client.probe()
    return result


@router.get("/api/channels/protocol/config")
async def get_channels_protocol_config():
    """读取视频号本机微信协议通道可视化配置(不包含 Cookie 明文或口令 URL)。"""
    app_runtime.channels_proxy.configure_data_dir(Path(app_runtime.cfg.db_path).resolve().parent)
    return {
        "config": export_channels_settings(app_runtime.cfg),
        "bridge": app_runtime.channels_bridge.status(include_processes=False),
        "proxy": app_runtime.channels_proxy.status(),
    }


@router.put("/api/channels/protocol/config", openapi_extra={
    "requestBody": {"content": {"application/json": {
        "schema": ChannelsProtocolPatch.model_json_schema()}}}})
async def put_channels_protocol_config(body: Any = Body(...)):
    """校验并保存视频号微信通道配置，立即对后续任务生效并重启保留。"""
    try:
        saved = save_channels_settings(app_runtime.cfg, body)
    except ValidationError as exc:
        details = exc.errors(include_url=False, include_context=False, include_input=False)
        for detail in details:
            detail["loc"] = ["body", *detail["loc"]]
        raise HTTPException(422, details) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        raise HTTPException(503, "视频号通道配置尚未保存，请稍后重试") from None
    if app_runtime.channels_proxy.running and not saved.get("proxy_enabled"):
        await app_runtime.channels_proxy.stop()
    return _channels_status_payload()


@router.delete("/api/channels/protocol/config")
async def delete_channels_protocol_config():
    """清除页面保存的视频号通道覆盖配置，恢复 config.yaml / 环境变量默认值。"""
    saved = reset_channels_settings(app_runtime.cfg)
    if app_runtime.channels_proxy.running and not saved.get("proxy_enabled"):
        await app_runtime.channels_proxy.stop()
    return _channels_status_payload()


@router.post("/api/channels/protocol/probe")
async def probe_channels_protocol(body: ChannelsProbeIn = Body(default_factory=ChannelsProbeIn)):
    """使用当前已保存配置或页面草稿参数，对本机微信 Finder 通道执行一次实时探活。"""
    mode = body.connection_mode
    use_proxy = False
    endpoint = ""
    endpoint_file = ""
    try:
        if mode == "none":
            return {
                "ok": False,
                "ready": False,
                "configured": False,
                "error": "当前选择未连接本机微信通道，仅可使用创作者助手管理本账号",
            }
        if mode == "proxy":
            use_proxy = True
        elif mode == "endpoint":
            endpoint = validate_channels_endpoint(body.cdp_endpoint or "")
            if not endpoint:
                return {
                    "ok": False,
                    "ready": False,
                    "configured": False,
                    "error": "请先填写本机回环 CDP 端点地址（如 http://127.0.0.1:9222）",
                }
        elif mode == "endpoint_file":
            endpoint_file = validate_channels_endpoint_file(body.cdp_endpoint_file or "")
            if not endpoint_file:
                return {
                    "ok": False,
                    "ready": False,
                    "configured": False,
                    "error": "请先填写 WMPF 控制口令文件路径",
                }
        else:
            if body.cdp_endpoint is None and body.cdp_endpoint_file is None:
                use_proxy = bool(getattr(app_runtime.cfg.engine, "channels_proxy_enabled", False))
            if not use_proxy:
                if body.cdp_endpoint is not None:
                    endpoint = validate_channels_endpoint(body.cdp_endpoint)
                if body.cdp_endpoint_file is not None:
                    endpoint_file = validate_channels_endpoint_file(body.cdp_endpoint_file)
                if body.cdp_endpoint is None and body.cdp_endpoint_file is None:
                    endpoint = str(getattr(app_runtime.cfg.engine, "channels_cdp_endpoint", "") or "").strip()
                    endpoint_file = str(getattr(app_runtime.cfg.engine, "channels_cdp_endpoint_file", "") or "").strip()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    if not use_proxy and not endpoint and not endpoint_file:
        return {
            "ok": False,
            "ready": False,
            "configured": False,
            "error": "尚未配置本地微信通道（可启用内置微信代理或填写 CDP 端点）",
        }

    timeout = max(15.0, float(getattr(app_runtime.cfg.engine, "request_timeout_seconds", 30.0) or 30.0))
    client = ChannelsProtocolClient(
        endpoint=endpoint,
        endpoint_file=endpoint_file,
        use_proxy=use_proxy,
        proxy_manager=app_runtime.channels_proxy if use_proxy else None,
        timeout=timeout,
    )
    probe_result = await client.probe()
    resp_payload: dict[str, Any] = {
        "ok": bool(probe_result.get("ok")),
        "ready": bool(probe_result.get("ready")),
        "configured": True,
        "probe": probe_result,
        "error": str(probe_result.get("error") or ""),
    }
    if use_proxy:
        resp_payload["proxy"] = app_runtime.channels_proxy.status()
    return resp_payload


@router.get("/api/channels/protocol/discover")
async def discover_channels_protocol():
    """扫描本机回环常见 CDP 端口及运行中的桌面微信/WMPF 进程。"""
    extra = []
    cur = str(getattr(app_runtime.cfg.engine, "channels_cdp_endpoint", "") or "").strip()
    if cur:
        extra.append(cur)
    endpoints, processes = await asyncio.gather(
        discover_local_cdp_endpoints(extra),
        asyncio.to_thread(discover_wechat_processes),
    )
    bridge_state = app_runtime.channels_bridge.status(include_processes=False)
    bridge_state["wechat_processes"] = processes
    return {
        "endpoints": endpoints,
        "wechat_processes": processes,
        "bridge": bridge_state,
    }


@router.post("/api/channels/protocol/bridge/start")
async def start_channels_protocol_bridge(
    body: ChannelsBridgeStartIn = Body(default_factory=ChannelsBridgeStartIn),
):
    """在 CreatorHub 内受管启动单会话 WMPF LoopbackBridge，并可选写入当前通道配置。"""
    app_runtime.channels_bridge.configure_data_dir(Path(app_runtime.cfg.db_path).resolve().parent)
    try:
        bridge_state = await app_runtime.channels_bridge.start(
            debug_port=body.debug_port,
            cdp_port=body.cdp_port,
            allowed_pids=body.allowed_pids,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(409, f"无法监听本机回环端口（可能已被占用）: {exc}") from exc

    if body.apply_to_config:
        if app_runtime.channels_proxy.running:
            await app_runtime.channels_proxy.stop()
        save_channels_settings(
            app_runtime.cfg,
            {
                "connection_mode": "endpoint_file",
                "channels_cdp_endpoint_file": str(app_runtime.channels_bridge.endpoint_file),
            },
        )
    payload = _channels_status_payload()
    payload["bridge"] = bridge_state
    return {"ok": True, **payload}


@router.post("/api/channels/protocol/bridge/stop")
async def stop_channels_protocol_bridge():
    """停止受管 WMPF LoopbackBridge 并清理其生成的临时控制口令文件。"""
    bridge_state = await app_runtime.channels_bridge.stop()
    cur_file = str(getattr(app_runtime.cfg.engine, "channels_cdp_endpoint_file", "") or "").strip()
    if cur_file:
        try:
            if Path(cur_file).expanduser().resolve() == app_runtime.channels_bridge.endpoint_file:
                save_channels_settings(app_runtime.cfg, {"channels_cdp_endpoint_file": ""})
        except OSError:
            pass
    payload = _channels_status_payload()
    payload["bridge"] = bridge_state
    return {"ok": True, **payload}


@router.post("/api/channels/protocol/proxy/start")
async def start_channels_protocol_proxy(
    body: ChannelsProxyStartIn = Body(default_factory=ChannelsProxyStartIn),
):
    """一键检查/安装根证书、启动内置选择性本地视频号 HTTPS 代理并持久化下次自动恢复配置。"""
    app_runtime.channels_proxy.configure_data_dir(Path(app_runtime.cfg.db_path).resolve().parent)
    cert_installed_now = False
    if body.auto_install_cert and body.enable_system_proxy:
        ca_before = await asyncio.to_thread(app_runtime.channels_proxy.ca.trust_status)
        if not ca_before.get("trusted") and (
            sys.platform == "win32" or hasattr(app_runtime.channels_proxy.ca.install_trust, "assert_called")
        ):
            cert_res = await asyncio.to_thread(app_runtime.channels_proxy.ca.install_trust)
            if not cert_res.get("ok"):
                raise HTTPException(
                    409,
                    str(cert_res.get("error") or "尚未完成本地根证书信任授权，请在系统弹窗中点击「是」后重试"),
                )
            cert_installed_now = True

    try:
        proxy_state = await app_runtime.channels_proxy.start(
            proxy_port=body.proxy_port,
            enable_system_proxy_flag=body.enable_system_proxy,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, ChannelsProtocolTransportError) as exc:
        raise HTTPException(409, str(exc)) from exc

    if body.apply_to_config:
        if app_runtime.channels_bridge.status(include_processes=False).get("running"):
            await app_runtime.channels_bridge.stop()
        persisted_port = (
            int(proxy_state.get("proxy_port") or 2023)
            if int(body.proxy_port) == 0
            else int(body.proxy_port)
        )
        if not (1024 <= persisted_port <= 65535):
            persisted_port = int(getattr(app_runtime.cfg.engine, "channels_proxy_port", 2023) or 2023)
        save_channels_settings(
            app_runtime.cfg,
            {
                "connection_mode": "proxy",
                "channels_proxy_port": persisted_port,
                "channels_proxy_manage_sysproxy": bool(body.enable_system_proxy),
            },
        )
    payload = _channels_status_payload()
    payload["proxy"] = proxy_state
    payload["cert_installed_now"] = cert_installed_now
    return {"ok": True, **payload}


@router.post("/api/channels/protocol/proxy/stop")
async def stop_channels_protocol_proxy(
    body: ChannelsProxyStopIn = Body(default_factory=ChannelsProxyStopIn),
):
    """停止内置视频号代理并立即恢复退出前的 Windows 系统代理配置。"""
    proxy_state = await app_runtime.channels_proxy.stop()
    if body.disable_auto_start and bool(getattr(app_runtime.cfg.engine, "channels_proxy_enabled", False)):
        save_channels_settings(app_runtime.cfg, {"connection_mode": "none"})
    payload = _channels_status_payload()
    payload["proxy"] = proxy_state
    return {"ok": True, **payload}


@router.post("/api/channels/protocol/wechat/open")
async def open_channels_desktop_wechat():
    """唤起或启动本机桌面微信主窗口，免除浏览器自定义协议确认弹窗。"""
    res = await asyncio.to_thread(open_or_focus_wechat)
    return {**res, "proxy": app_runtime.channels_proxy.status()}


@router.post("/api/channels/protocol/wechat/restart")
async def restart_channels_desktop_wechat():
    """一键重启本机桌面微信客户端以立即应用新的系统代理配置。"""
    res = await asyncio.to_thread(restart_wechat)
    if res.get("ok"):
        app_runtime.channels_proxy.clear_wechat_preexisting()
    return {**res, "proxy": app_runtime.channels_proxy.status()}


@router.post("/api/channels/protocol/proxy/cert/install")
async def install_channels_proxy_cert():
    """将本机自签名 CA 根证书安装到当前用户受信任根证书存储（需用户在系统弹窗确认）。"""
    app_runtime.channels_proxy.configure_data_dir(Path(app_runtime.cfg.db_path).resolve().parent)
    res = await asyncio.to_thread(app_runtime.channels_proxy.ca.install_trust)
    return {
        **res,
        "proxy": app_runtime.channels_proxy.status(),
    }


@router.post("/api/channels/protocol/proxy/cert/uninstall")
async def uninstall_channels_proxy_cert():
    """从当前用户受信任根证书存储移除本机自签名 CA 根证书。"""
    app_runtime.channels_proxy.configure_data_dir(Path(app_runtime.cfg.db_path).resolve().parent)
    res = await asyncio.to_thread(app_runtime.channels_proxy.ca.uninstall_trust)
    return {
        **res,
        "proxy": app_runtime.channels_proxy.status(),
    }


@router.get("/api/channels/protocol/proxy/ca.cer")
async def download_channels_proxy_ca():
    """导出本地自签名 CA 公钥证书文件（仅导出公钥 .cer，绝不导出私钥 .key）。"""
    app_runtime.channels_proxy.configure_data_dir(Path(app_runtime.cfg.db_path).resolve().parent)
    cert_path, _, _ = app_runtime.channels_proxy.ca.ensure_ca()
    return Response(
        content=cert_path.read_bytes(),
        media_type="application/x-x509-ca-cert",
        headers={
            "Content-Disposition": 'attachment; filename="creatorhub-channels-ca.cer"',
            "Cache-Control": "no-store",
        },
    )


class ChannelsCreatorResolveIn(BaseModel):
    target: str


@router.post("/api/channels/resolve-creator")
async def resolve_channels_creator_endpoint(body: ChannelsCreatorResolveIn):
    """把视频号主页链接、finderUsername 或单条作品分享链反查为创作者 finderUsername。"""
    res = await collections_service._resolve_channels_creator_target(body.target)
    return {
        "ok": True,
        "username": res.username,
        "nickname": res.nickname,
        "avatar": res.avatar,
        "source_kind": res.source_kind,
    }
