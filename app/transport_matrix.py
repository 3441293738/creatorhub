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
     "setting": "douyin_read_mode", "api": True, "browser": True},
    {"platform": "douyin", "operation": "public_monitor", "label": "公开作品/评论/弹幕监控",
     "setting": "douyin_read_mode", "api": True, "browser": True},
    {"platform": "douyin", "operation": "account_works", "label": "本账号作品同步",
     "setting": "douyin_read_mode", "api": True, "browser": True},
    {"platform": "douyin", "operation": "own_work_comments", "label": "本账号作品评论抓取",
     "setting": "douyin_read_mode", "api": True, "browser": True},
    {"platform": "douyin", "operation": "following_list", "label": "关注列表同步",
     "setting": "douyin_read_mode", "api": True, "browser": True},
    # The current Web follower endpoint returns an unclassifiable empty body for
    # authenticated accounts, so browser interception is the only supported path.
    {"platform": "douyin", "operation": "followers_list", "label": "粉丝列表同步",
     "setting": "douyin_read_mode", "api": False, "browser": True,
     "note": "当前网页 API 不稳定，使用账号浏览器拦截"},
    {"platform": "douyin", "operation": "dm_sync", "label": "私信会话/历史同步",
     "setting": "", "fixed": "browser", "api": False, "browser": True,
     "note": "会话发现和历史读取依赖账号浏览器上下文"},
    # Douyin writes controlled by douyin_write_mode.
    {"platform": "douyin", "operation": "comment_write", "label": "评论/回复发送",
     "setting": "douyin_write_mode", "api": True, "browser": True},
    {"platform": "douyin", "operation": "follow_write", "label": "关注/取关",
     "setting": "douyin_write_mode", "api": True, "browser": True},
    {"platform": "douyin", "operation": "dm_send", "label": "已有会话私信发送",
     "setting": "douyin_write_mode", "api": True, "browser": True},
    {"platform": "douyin", "operation": "publish", "label": "发布作品",
     "setting": "", "fixed": "browser", "api": False, "browser": True,
     "note": "创作中心页面流程"},

    # XHS modes that are actually configurable today.
    {"platform": "xhs", "operation": "public_monitor", "label": "作品/关键词/公开评论读取",
     "setting": "xhs_read_mode", "api": True, "browser": True},
    {"platform": "xhs", "operation": "comment_write", "label": "评论/回复发送",
     "setting": "xhs_comment_write_mode", "api": True, "browser": True,
     "manual": True, "native_browser_override": True},
    {"platform": "xhs", "operation": "publish", "label": "发布作品",
     "setting": "xhs_publish_mode", "api": True, "browser": True,
     "native_browser_override": True},
    {"platform": "xhs", "operation": "follows", "label": "关注/粉丝列表",
     "setting": "", "fixed": "unavailable", "api": False, "browser": False,
     "note": "网页端未提供该列表"},
    # Platforms without a supported direct transport remain explicit rather
    # than being silently grouped under a global API selector.
    {"platform": "kuaishou", "operation": "account_management", "label": "作品/评论/关注/写入",
     "setting": "", "fixed": "browser", "api": False, "browser": True},
    {"platform": "shipinhao", "operation": "account_management", "label": "作品/评论/发布",
     "setting": "", "fixed": "browser", "api": False, "browser": True},
)


def _setting_value(cfg: Any, name: str, default: str = "browser") -> str:
    engine = getattr(cfg, "engine", cfg)
    return str(getattr(engine, name, default) or default).strip().lower()


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
    configured = str(spec.get("fixed") or (
        _setting_value(cfg, setting) if setting else "browser"))
    effective = configured
    reason = ""

    if (spec.get("native_browser_override") and account is not None
            and getattr(account, "identity_mode", "") == "native"
            and configured == "api"):
        effective = "browser"
        reason = "原生身份账号固定使用浏览器通道"
    elif configured == "manual" and spec.get("manual"):
        effective = "manual"
    elif configured == "hybrid":
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

    fallback = (
        "browser_on_confirmed_failure" if effective == "hybrid" else "none")
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
    return {
        "settings": {
            "douyin_read_mode": _setting_value(cfg, "douyin_read_mode", "hybrid"),
            "douyin_write_mode": _setting_value(cfg, "douyin_write_mode", "browser"),
            "xhs_read_mode": _setting_value(cfg, "xhs_read_mode", "browser"),
            "xhs_publish_mode": _setting_value(cfg, "xhs_publish_mode", "browser"),
            "xhs_comment_write_mode": _setting_value(
                cfg, "xhs_comment_write_mode", "browser"),
        },
        "rows": rows,
        "accounts": isolation,
    }
