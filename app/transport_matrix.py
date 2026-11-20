"""Transport capability matrix and per-account isolation diagnostics.

The matrix is the single source of truth for UI labels and routes that must
honour an explicit API-only/browser-only selection.  It deliberately contains
no credentials or proxy addresses.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


TRANSPORT_SPECS: tuple[dict[str, Any], ...] = (
    # Douyin reads controlled by douyin_read_mode.
    {"platform": "douyin", "operation": "keyword_collection", "label": "关键词采集",
     "setting": "douyin_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "note": "Hybrid 先直连 search/single API；普通兼容性失败可回退浏览器，verification_required/风控时进入冷却且不打开浏览器"},
    {"platform": "douyin", "operation": "public_monitor", "label": "公开作品/评论/弹幕监控",
     "setting": "douyin_read_mode", "api": True, "browser": True,
     "auth_protocol": "a_bogus + account-bound webSign",
     "wire_protocol": "JSON / WebSocket protobuf"},
    {"platform": "douyin", "operation": "account_works", "label": "本账号作品同步",
     "setting": "douyin_read_mode", "api": True, "browser": True},
    {"platform": "douyin", "operation": "share_download", "label": "通用链接下载详情",
     "setting": "douyin_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "note": "默认优先 API，普通读取失败时回退账号浏览器；登录失效或安全验证时停止"},
    {"platform": "douyin", "operation": "own_work_comments", "label": "本账号作品评论抓取",
     "setting": "douyin_read_mode", "api": True, "browser": True},
    {"platform": "douyin", "operation": "following_list", "label": "关注列表同步",
     "setting": "douyin_read_mode", "api": True, "browser": True},
    {"platform": "douyin", "operation": "account_profile", "label": "账号资料/登录态体检",
     "setting": "douyin_profile_mode", "api": True, "browser": True},
    {"platform": "douyin", "operation": "followers_list", "label": "粉丝列表同步",
     "setting": "douyin_followers_mode", "api": True, "browser": True,
     "note": "直连接口受账号风控影响时，混合模式会回退浏览器"},
    {"platform": "douyin", "operation": "dm_sync", "label": "私信会话/历史同步",
     "setting": "douyin_dm_sync_mode", "api": True, "browser": True,
     "auth_protocol": "account Cookie", "wire_protocol": "imapi protobuf",
     "note": "API 使用 imapi protobuf 初始化会话并按需读取历史"},
    {"platform": "douyin", "operation": "creator_danmaku", "label": "本账号创作中心弹幕",
     "setting": "douyin_creator_danmaku_mode", "api": True, "browser": True,
     "note": "API 按本账号作品 ID 读取弹幕；浏览器模式保留创作中心流程"},
    # Douyin writes controlled by douyin_write_mode.
    {"platform": "douyin", "operation": "comment_write", "label": "评论/回复发送",
     "setting": "douyin_write_mode", "api": True, "browser": True,
     "hybrid_order": "api_first"},
    {"platform": "douyin", "operation": "follow_write", "label": "关注/取关",
     "setting": "douyin_write_mode", "api": True, "browser": True,
     "hybrid_order": "api_first"},
    {"platform": "douyin", "operation": "dm_send", "label": "已有会话私信发送",
     "setting": "douyin_write_mode", "api": True, "browser": True,
     "hybrid_order": "api_first", "auth_protocol": "account Cookie",
     "wire_protocol": "imapi protobuf"},
    {"platform": "douyin", "operation": "publish", "label": "发布作品",
     "setting": "douyin_publish_mode", "api": False, "browser": True,
     "note": "上传鉴权和提交签名仍依赖创作中心页面"},

    # XHS modes that are actually configurable today.
    {"platform": "xhs", "operation": "public_monitor", "label": "作品/关键词/公开评论读取",
     "setting": "xhs_read_mode", "api": True, "browser": True,
     "auth_protocol": "x-s / x-t / x-s-common + a1",
     "wire_protocol": "JSON", "signer_runtime": "persistent_node"},
    {"platform": "xhs", "operation": "comment_write", "label": "评论/回复发送",
     "setting": "xhs_comment_write_mode", "api": True, "browser": True,
     "manual": True, "native_browser_override": True},
    {"platform": "xhs", "operation": "publish", "label": "发布作品",
     "setting": "xhs_publish_mode", "api": True, "browser": True,
     "native_browser_override": True,
     "auth_protocol": "x-s / x-t / x-rap-param + COS HMAC-SHA1",
     "wire_protocol": "JSON + binary upload"},
    {"platform": "xhs", "operation": "follows", "label": "关注/粉丝列表",
     "setting": "", "fixed": "unavailable", "api": False, "browser": False,
     "note": "网页端未提供该列表"},
    # Rednote (海外小红书): strictly isolated rednote.com 4-domain boundary + XYS/XYW+b1 API & RPA.
    {"platform": "rednote", "operation": "public_monitor", "label": "作品/关键词/收藏与公开评论读取",
     "setting": "rednote_read_mode", "default": "hybrid", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "rednote.com Cookie + XYS / XYW + b1 / dsllt + a1",
     "wire_protocol": "HTTPS JSON + SSR __INITIAL_STATE__"},
    {"platform": "rednote", "operation": "account_works", "label": "本账号资料/作品同步与可见性删除管理",
     "setting": "rednote_read_mode", "default": "hybrid", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "webapi.rednote.com + creator.rednote.com XYS/XYW + b1",
     "wire_protocol": "HTTPS JSON"},
    {"platform": "rednote", "operation": "publish", "label": "图文与视频笔记发布",
     "setting": "rednote_publish_mode", "default": "browser", "api": True, "browser": True,
     "auth_protocol": "creator.rednote.com RPA / 实验性 XYS + b1 + x-rap-param (//webapi.rednote.com)",
     "wire_protocol": "COS upload + HTTPS JSON"},
    {"platform": "rednote", "operation": "comment_write", "label": "评论写入与自动计划",
     "setting": "", "fixed": "unavailable", "api": False, "browser": False,
     "note": "海外小红书暂未开放评论写入与自动计划"},
    {"platform": "rednote", "operation": "dm_sync", "label": "私信会话/自动回复",
     "setting": "", "fixed": "unavailable", "api": False, "browser": False,
     "note": "海外小红书暂未开放国内 RWP 私信协议通道"},
    {"platform": "rednote", "operation": "follows", "label": "关注/粉丝列表",
     "setting": "", "fixed": "unavailable", "api": False, "browser": False,
     "note": "网页端未提供该列表"},
    # Platforms without a supported direct transport remain explicit rather
    # than being silently grouped under a global API selector.
    {"platform": "kuaishou", "operation": "account_management", "label": "作品/评论/关注/写入",
     "setting": "", "fixed": "browser", "api": False, "browser": True},
    {"platform": "shipinhao", "operation": "account_management", "label": "作品/评论/发布",
     "setting": "", "fixed": "browser", "api": False, "browser": True,
     "auth_protocol": "X-WECHAT-UIN + device fingerprint + plugin session",
     "wire_protocol": "JSON / form requests"},
    {"platform": "shipinhao", "operation": "finder_protocol", "label": "搜索/创作者/全量评论协议采集",
     "setting": "channels_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "requires_channels_cdp": True,
     "auth_protocol": "WeixinJSBridge finderH5ExtTransfer (cmdid 10753) + WMPF loopback CDP",
     "wire_protocol": "Loopback CDP / WMPF protobuf + JSON",
     "note": "auto 模式优先复用已登录桌面微信原生 Finder 服务，不可达时回退创作者助手页面"},
    {"platform": "shipinhao", "operation": "share_download", "label": "分享解析与加密视频 WxIsaac64 解码",
     "setting": "channels_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "requires_channels_cdp": True,
     "auth_protocol": "finderGetCommentDetail + finder-preview API + WxIsaac64 128KiB XOR",
     "wire_protocol": "HTTPS JSON + encrypted MP4 stream"},
    # Bilibili WBI / Cookie / Broadcast Protobuf + member.bilibili.com RPA.
    {"platform": "bilibili", "operation": "public_monitor", "label": "空间/搜索/评论/弹幕读取",
     "setting": "bilibili_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "WBI w_rid/wts + buvid3/4 + SESSDATA",
     "wire_protocol": "HTTPS JSON + deflate XML danmaku"},
    {"platform": "bilibili", "operation": "account_works", "label": "本账号资料/创作中心稿件/统计",
     "setting": "bilibili_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "SESSDATA + bili_jct + RSA-OAEP correspond refresh",
     "wire_protocol": "HTTPS JSON"},
    {"platform": "bilibili", "operation": "following_list", "label": "关注列表同步",
     "setting": "bilibili_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "SESSDATA + /x/relation/followings",
     "wire_protocol": "HTTPS JSON"},
    {"platform": "bilibili", "operation": "followers_list", "label": "粉丝列表同步",
     "setting": "bilibili_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "SESSDATA + /x/relation/followers",
     "wire_protocol": "HTTPS JSON"},
    {"platform": "bilibili", "operation": "dm_sync", "label": "私信会话/历史与 Broadcast 推送",
     "setting": "bilibili_read_mode", "api": True, "browser": False,
     "auth_protocol": "SESSDATA + WBI new_sessions + Broadcast AuthReq",
     "wire_protocol": "HTTPS JSON + WSS 8-type Protobuf WatchNotify"},
    {"platform": "bilibili", "operation": "comment_write", "label": "评论/回复/点赞/删除与关注",
     "setting": "bilibili_write_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "bili_jct CSRF + SESSDATA",
     "wire_protocol": "x-www-form-urlencoded"},
    {"platform": "bilibili", "operation": "dm_send", "label": "私信发送",
     "setting": "bilibili_write_mode", "api": True, "browser": False,
     "auth_protocol": "WBI (w_sender_uid/w_receiver_id/w_dev_id) + bili_jct",
     "wire_protocol": "x-www-form-urlencoded"},
    {"platform": "bilibili", "operation": "publish", "label": "视频投稿与图文动态发布",
     "setting": "", "fixed": "browser", "api": False, "browser": True,
     "auth_protocol": "member.bilibili.com RPA + add/v3 payload merge",
     "wire_protocol": "UPOS + HTTPS JSON"},
    # Instagram Mobile API v1 (Bearer IGT:2 + SIGNATURE.) + Web Session HTTP + Web RPA.
    {"platform": "instagram", "operation": "public_monitor", "label": "主页/话题/Reels/评论读取",
     "setting": "instagram_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "Bearer IGT:2 + Web Cookie / X-CSRFToken",
     "wire_protocol": "HTTPS JSON + chunked NDJSON (clips/user)"},
    {"platform": "instagram", "operation": "account_works", "label": "本账号资料与作品列表同步",
     "setting": "instagram_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "Bearer IGT:2 + response header token writeback",
     "wire_protocol": "HTTPS JSON"},
    {"platform": "instagram", "operation": "following_list", "label": "关注列表同步",
     "setting": "instagram_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "Bearer IGT:2 + RankTokenCache (/friendships/following/)",
     "wire_protocol": "HTTPS JSON"},
    {"platform": "instagram", "operation": "followers_list", "label": "粉丝列表同步",
     "setting": "instagram_read_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "Bearer IGT:2 + RankTokenCache (/friendships/followers/)",
     "wire_protocol": "HTTPS JSON"},
    {"platform": "instagram", "operation": "dm_sync", "label": "Direct v2 私信收件箱与历史同步",
     "setting": "instagram_read_mode", "api": True, "browser": False,
     "auth_protocol": "Bearer IGT:2 + Web direct_v2/inbox fallback",
     "wire_protocol": "HTTPS JSON (microsecond timestamp)"},
    {"platform": "instagram", "operation": "comment_write", "label": "评论/回复/点赞与关注",
     "setting": "instagram_write_mode", "api": True, "browser": True,
     "hybrid_order": "api_first",
     "auth_protocol": "signed_body=SIGNATURE. + HMAC-SHA256 user_breadcrumb",
     "wire_protocol": "x-www-form-urlencoded"},
    {"platform": "instagram", "operation": "dm_send", "label": "Direct v2 私信发送(文本/链接/图视)",
     "setting": "instagram_write_mode", "api": True, "browser": False,
     "auth_protocol": "Bearer IGT:2 + unique_time_id client_context",
     "wire_protocol": "x-www-form-urlencoded (broadcast/text | link | configure_*)"},
    {"platform": "instagram", "operation": "publish", "label": "图文/轮播/Reels/快拍发布",
     "setting": "instagram_write_mode", "api": True, "browser": True,
     "hybrid_order": "api_first", "native_browser_override": True,
     "auth_protocol": "rupload_igphoto/igvideo + %20 SIGNATURE. configure + Web RPA",
     "wire_protocol": "Binary stream + x-www-form-urlencoded"},
)


def _setting_value(cfg: Any, name: str, default: str = "browser") -> str:
    engine = getattr(cfg, "engine", cfg)
    return str(getattr(engine, name, default) or default).strip().lower()


def _channels_cdp_readiness(cfg: Any) -> dict[str, Any]:
    engine = getattr(cfg, "engine", cfg)
    proxy_enabled = bool(getattr(engine, "channels_proxy_enabled", False))
    endpoint = str(getattr(engine, "channels_cdp_endpoint", "") or "").strip()
    endpoint_file = str(getattr(engine, "channels_cdp_endpoint_file", "") or "").strip()
    if proxy_enabled:
        from .platforms.channels.proxy_bridge import get_active_proxy_manager

        mgr = get_active_proxy_manager()
        if mgr is None or not mgr.running:
            return {
                "configured": True,
                "ready": False,
                "source": "proxy",
                "error": "已启用内置视频号代理模式，但本地代理尚未启动",
            }
        st = mgr.status()
        if int(st.get("ready_pages") or 0) > 0:
            return {
                "configured": True,
                "ready": True,
                "source": "proxy",
                "error": "",
            }
        if int(st.get("connected_pages") or 0) > 0:
            return {
                "configured": True,
                "ready": False,
                "source": "proxy",
                "error": "已连接微信视频号页面，正在等待协议运行时加载完成",
            }
        return {
            "configured": True,
            "ready": False,
            "source": "proxy",
            "error": "内置视频号代理已运行，请在桌面微信中打开一次视频号页面",
        }
    if endpoint_file:
        from .platforms.channels.wmpf_cdp import inspect_endpoint_file

        info = inspect_endpoint_file(endpoint_file)
        ready = bool(info.get("exists") and info.get("valid"))
        return {
            "configured": True,
            "ready": ready,
            "source": "endpoint_file",
            "error": "" if ready else str(info.get("error") or "口令文件不可用"),
        }
    if endpoint:
        return {
            "configured": True,
            "ready": True,
            "source": "endpoint",
            "error": "",
        }
    return {
        "configured": False,
        "ready": False,
        "source": "none",
        "error": "",
    }


def transport_spec(platform: str, operation: str) -> dict[str, Any]:
    for spec in TRANSPORT_SPECS:
        if spec["platform"] == platform and spec["operation"] == operation:
            return spec
    raise KeyError(f"unknown transport operation: {platform}/{operation}")


def resolve_transport(cfg: Any, platform: str, operation: str,
                      account: Any = None) -> dict[str, Any]:
    """Return configured and effective transport for one operation."""
    spec = transport_spec(platform, operation)
    setting = str(spec.get("setting") or "")
    fallback_default = str(spec.get("default") or "browser")
    configured = str(spec.get("fixed") or (
        _setting_value(cfg, setting, fallback_default) if setting else fallback_default))
    effective = configured
    reason = ""

    if (spec.get("native_browser_override") and account is not None
            and getattr(account, "identity_mode", "") == "native"
            and configured == "api"):
        effective = "browser"
        reason = "原生身份账号固定使用浏览器通道"
    elif configured in {"protocol", "native"}:
        if spec.get("api"):
            effective = "api"
        else:
            effective = "unavailable"
            reason = str(spec.get("note") or "该操作不支持原生协议")
    elif configured == "manual" and spec.get("manual"):
        effective = "manual"
    elif configured in {"hybrid", "auto"}:
        if spec.get("api") and spec.get("browser"):
            effective = "hybrid"
        elif spec.get("api"):
            effective = "api"
        elif spec.get("browser"):
            effective = "browser"
            reason = str(spec.get("note") or "该操作仅支持浏览器")
        else:
            effective = "unavailable"
    elif configured == "api" and not spec.get("api"):
        effective = "unavailable"
        reason = str(spec.get("note") or "该操作不支持 API")
    elif configured == "browser" and not spec.get("browser"):
        effective = "unavailable"
        reason = str(spec.get("note") or "该操作不支持浏览器")
    elif configured not in {"api", "browser", "manual", "unavailable"}:
        effective = "unavailable"
        reason = "配置值无效"

    if spec.get("requires_channels_cdp") and effective not in {"browser", "unavailable"}:
        readiness = _channels_cdp_readiness(cfg)
        if not readiness["ready"]:
            prefix = f"{readiness['error']}；" if readiness["error"] else "未配置本机微信 CDP 端点；"
            if configured in {"protocol", "native"}:
                effective = "unavailable"
                reason = readiness["error"] or "未配置本机微信 Finder CDP 端点，请在「设置 → 视频号微信通道」配置"
            elif operation == "finder_protocol":
                effective = "browser"
                reason = prefix + "当前仅回退创作者助手读取本账号作品与评论"
            else:
                effective = "api"
                reason = prefix + "分享解析仅走公开预览接口（加密视频解码需连接微信通道）"
        elif not reason:
            if readiness["source"] == "proxy":
                reason = "已通过内置本机代理连接桌面微信视频号协议通道"
            elif readiness["source"] == "endpoint_file":
                reason = "已就绪 WMPF 控制口令文件，可连接本机微信 Finder 协议"
            else:
                reason = "已配置本机微信 Finder CDP 端点"

    fallback = (
        "browser_on_confirmed_failure" if effective == "hybrid" else "none")
    if effective == "hybrid" and spec.get("hybrid_order") == "browser_first":
        fallback = "api_on_inconclusive_browser_failure"
    return {
        **spec,
        "configured_mode": configured,
        "effective_mode": effective,
        "fallback": fallback,
        "opens_browser": effective in {"browser", "hybrid"},
        "reason": reason,
    }


def douyin_client_environment(source: Any) -> dict[str, Any]:
    """Map an account/Identity to direct-request browser parameters."""
    locale = str(getattr(source, "locale", "") or "zh-CN")
    accept = str(getattr(source, "fp_accept_languages", "") or "").strip()
    if not accept:
        accept = f"{locale},{locale.split('-', 1)[0]};q=0.9"
    return {
        "locale": locale,
        "accept_language": accept,
        "screen_width": max(320, int(getattr(source, "viewport_w", 0) or 1536)),
        "screen_height": max(240, int(getattr(source, "viewport_h", 0) or 864)),
    }


def _normalized_path(value: str) -> str:
    if not value:
        return ""
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _state_fingerprint(account: Any) -> str:
    raw = str(getattr(account, "storage_state", "") or
              getattr(account, "creator_storage_state", "") or "")
    if not raw:
        return ""
    # Normalize JSON when possible so formatting differences do not hide a
    # duplicate login state.  Only the digest is retained.
    try:
        raw = json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    except (TypeError, ValueError):
        pass
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_transport_matrix(cfg: Any, accounts: Iterable[Any]) -> dict[str, Any]:
    accounts = list(accounts)
    rows = [resolve_transport(cfg, spec["platform"], spec["operation"])
            for spec in TRANSPORT_SPECS]
    profile_keys = [_normalized_path(str(getattr(a, "profile_dir", "") or ""))
                    for a in accounts]
    proxy_keys = [str(getattr(a, "proxy", "") or "").strip() for a in accounts]
    state_keys = [_state_fingerprint(a) for a in accounts]
    profile_counts = Counter(value for value in profile_keys if value)
    proxy_counts = Counter(value for value in proxy_keys if value)
    state_counts = Counter(value for value in state_keys if value)
    isolation = []
    for account, profile_key, proxy_key, state_key in zip(
            accounts, profile_keys, proxy_keys, state_keys):
        profile_isolated = bool(profile_key and profile_counts[profile_key] == 1)
        credential_isolated = bool(state_key and state_counts[state_key] == 1)
        network_isolated = bool(proxy_key and proxy_counts[proxy_key] == 1)
        environment_seed = "|".join((
            str(getattr(account, "id", "") or ""), state_key,
            str(getattr(account, "ua", "") or ""), proxy_key,
            str(getattr(account, "locale", "") or ""),
            str(getattr(account, "viewport_w", "") or ""),
            str(getattr(account, "viewport_h", "") or ""),
        ))
        warnings: list[str] = []
        if not profile_isolated:
            warnings.append("浏览器 Profile 缺失或重复")
        if not credential_isolated:
            warnings.append("登录态缺失或与其他账号重复")
        if not network_isolated:
            warnings.append("API 与浏览器仍共享本机/重复代理出口")
        isolation.append({
            "account_id": getattr(account, "id", None),
            "platform": str(getattr(account, "platform", "") or ""),
            "nickname": str(getattr(account, "nickname", "") or ""),
            "status": str(getattr(account, "status", "") or ""),
            "profile_isolated": profile_isolated,
            "credential_isolated": credential_isolated,
            "api_session_isolated": True,
            "network_isolated": network_isolated,
            "network_scope": "专属代理" if network_isolated else "共享出口",
            "api_environment_aligned": bool(
                getattr(account, "ua", "") and getattr(account, "locale", "")
                and getattr(account, "viewport_w", 0)
                and getattr(account, "viewport_h", 0)),
            "environment_id": hashlib.sha256(
                environment_seed.encode("utf-8")).hexdigest()[:12],
            "warnings": warnings,
        })
    channels_readiness = _channels_cdp_readiness(cfg)
    return {
        "settings": {
            "douyin_read_mode": _setting_value(cfg, "douyin_read_mode", "hybrid"),
            "douyin_profile_mode": _setting_value(cfg, "douyin_profile_mode", "hybrid"),
            "douyin_followers_mode": _setting_value(cfg, "douyin_followers_mode", "hybrid"),
            "douyin_dm_sync_mode": _setting_value(cfg, "douyin_dm_sync_mode", "hybrid"),
            "douyin_creator_danmaku_mode": _setting_value(
                cfg, "douyin_creator_danmaku_mode", "hybrid"),
            "douyin_publish_mode": _setting_value(cfg, "douyin_publish_mode", "browser"),
            "douyin_write_mode": _setting_value(cfg, "douyin_write_mode", "hybrid"),
            "xhs_read_mode": _setting_value(cfg, "xhs_read_mode", "api"),
            "rednote_read_mode": _setting_value(cfg, "rednote_read_mode", "hybrid"),
            "rednote_publish_mode": _setting_value(cfg, "rednote_publish_mode", "browser"),
            "xhs_publish_mode": _setting_value(cfg, "xhs_publish_mode", "browser"),
            "xhs_comment_write_mode": _setting_value(
                cfg, "xhs_comment_write_mode", "browser"),
            "channels_read_mode": _setting_value(cfg, "channels_read_mode", "auto"),
            "bilibili_read_mode": _setting_value(cfg, "bilibili_read_mode", "hybrid"),
            "bilibili_write_mode": _setting_value(cfg, "bilibili_write_mode", "hybrid"),
            "instagram_read_mode": _setting_value(cfg, "instagram_read_mode", "hybrid"),
            "instagram_write_mode": _setting_value(cfg, "instagram_write_mode", "hybrid"),
            "channels_cdp_configured": channels_readiness["configured"],
            "channels_cdp_ready": channels_readiness["ready"],
            "channels_cdp_source": channels_readiness["source"],
        },
        "rows": rows,
        "accounts": isolation,
    }
