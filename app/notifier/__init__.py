"""通知推送。对应逆向 internal/notifier(bark / dingding / telegram)。

每个渠道 config 字段:
  bark:     {"key": "设备key", "server": "https://api.day.app"(可选)}
  dingtalk: {"webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
             "secret": "加签密钥"(可选), "keyword": "关键词"(可选)}
  telegram: {"bot_token": "xxx", "chat_id": "xxx", "api_base": "https://api.telegram.org"(可选)}
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Dict, List, Tuple

import httpx

TIMEOUT = 12.0


def _is_http_url(value: str) -> bool:
    parts = urllib.parse.urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def validate_channel_config(ch_type: str, config: dict | None) -> List[str]:
    """返回渠道配置的问题列表；空列表表示可用。纯函数，不触发网络。"""
    cfg = config or {}
    if ch_type not in CHANNEL_TYPES:
        return [f"未知渠道类型: {ch_type}"]

    errors: List[str] = []
    if ch_type == "bark":
        if not (cfg.get("key") or "").strip():
            errors.append("缺少 bark key")
        server = (cfg.get("server") or "").strip()
        if server and not _is_http_url(server):
            errors.append("bark server 必须是 http(s) 地址")
    elif ch_type == "dingtalk":
        webhook = (cfg.get("webhook") or "").strip()
        if not webhook:
            errors.append("缺少 webhook")
        elif not _is_http_url(webhook):
            errors.append("webhook 必须是 http(s) 地址")
    elif ch_type == "telegram":
        if not (cfg.get("bot_token") or "").strip():
            errors.append("缺少 bot_token")
        if not (cfg.get("chat_id") or "").strip():
            errors.append("缺少 chat_id")
        api_base = (cfg.get("api_base") or "").strip()
        if api_base and not _is_http_url(api_base):
            errors.append("api_base 必须是 http(s) 地址")
    return errors


def _dingtalk_sign(secret: str, timestamp_ms: str) -> str:
    """按钉钉机器人加签规则生成 URL 安全签名，结果可由同一输入复现。"""
    raw = base64.b64encode(hmac.new(
        secret.encode(), f"{timestamp_ms}\n{secret}".encode(), hashlib.sha256).digest())
    return urllib.parse.quote_plus(raw)


async def _send_bark(cfg: dict, title: str, text: str) -> Tuple[bool, str]:
    key = (cfg.get("key") or "").strip()
    if not key:
        return False, "缺少 bark key"
    server = (cfg.get("server") or "https://api.day.app").rstrip("/")
    url = f"{server}/{key}/{urllib.parse.quote(title)}/{urllib.parse.quote(text)}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(url, params={"group": "douyin-monitor"})
        return (r.status_code == 200), f"HTTP {r.status_code}"


async def _send_dingtalk(cfg: dict, title: str, text: str) -> Tuple[bool, str]:
    webhook = (cfg.get("webhook") or "").strip()
    if not webhook:
        return False, "缺少 webhook"
    secret = (cfg.get("secret") or "").strip()
    keyword = (cfg.get("keyword") or "").strip()
    if secret:
        ts = str(round(time.time() * 1000))
        webhook += f"&timestamp={ts}&sign={_dingtalk_sign(secret, ts)}"
    content = f"{keyword} {title}\n{text}".strip()
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(webhook, json={"msgtype": "text", "text": {"content": content}})
        try:
            ok = r.json().get("errcode", -1) == 0
        except Exception:
            ok = False
        return ok, r.text[:120]


async def _send_telegram(cfg: dict, title: str, text: str) -> Tuple[bool, str]:
    token = (cfg.get("bot_token") or "").strip()
    chat_id = (cfg.get("chat_id") or "").strip()
    if not token or not chat_id:
        return False, "缺少 bot_token / chat_id"
    api = (cfg.get("api_base") or "https://api.telegram.org").rstrip("/")
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{api}/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": f"{title}\n{text}",
                               "disable_web_page_preview": True})
        return (r.status_code == 200), f"HTTP {r.status_code} {r.text[:80]}"


_SENDERS = {"bark": _send_bark, "dingtalk": _send_dingtalk, "telegram": _send_telegram}
CHANNEL_TYPES = tuple(_SENDERS.keys())


async def send_one(ch_type: str, config: dict, title: str, text: str) -> Tuple[bool, str]:
    fn = _SENDERS.get(ch_type)
    if not fn:
        return False, f"未知渠道类型: {ch_type}"
    try:
        return await fn(config, title, text)
    except Exception as e:
        return False, repr(e)


async def notify_all(channels: List[dict], title: str, text: str) -> List[dict]:
    """channels: [{type, config(dict)} ...]。逐个发送，单渠道失败不影响其它。"""
    results: List[dict] = []
    for ch in channels:
        ch_type = ch.get("type")
        try:
            ok, detail = await send_one(ch_type, ch.get("config") or {}, title, text)
        except Exception as e:
            ok, detail = False, repr(e)
        results.append({"type": ch_type, "ok": ok, "detail": detail})
    return results
