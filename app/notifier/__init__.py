"""消息通知推送模块（支持 Bark / 钉钉 / Telegram）。

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


async def _send_bark(cfg: dict, title: str, text: str) -> Tuple[bool, str]:
    key = (cfg.get("key") or "").strip()
    if not key:
        return False, "缺少 bark key"
    server = (cfg.get("server") or "https://api.day.app").rstrip("/")
    url = f"{server}/{key}/{urllib.parse.quote(title)}/{urllib.parse.quote(text)}"
    async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as c:
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
        sign = base64.b64encode(hmac.new(
            secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest())
        webhook += f"&timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"
    content = f"{keyword} {title}\n{text}".strip()
    async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as c:
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
    async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as c:
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


async def notify_all(channels: List[dict], title: str, text: str):
    """channels: [{type, config(dict)} ...]。逐个发送,失败不影响其它。"""
    for ch in channels:
        await send_one(ch["type"], ch.get("config") or {}, title, text)
