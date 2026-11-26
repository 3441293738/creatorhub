import asyncio
import base64
import hashlib
import hmac
import urllib.parse
from unittest.mock import patch

import app.notifier as notifier


def test_validate_bark_valid_and_missing_key():
    assert notifier.validate_channel_config("bark", {"key": "abc"}) == []
    errors = notifier.validate_channel_config("bark", {})
    assert errors == ["缺少 bark key"]


def test_validate_dingtalk_requires_http_webhook():
    ok = {"webhook": "https://oapi.dingtalk.com/robot/send?access_token=x"}
    assert notifier.validate_channel_config("dingtalk", ok) == []
    assert notifier.validate_channel_config(
        "dingtalk", {"webhook": "http://example.com/robot"}
    ) == []
    assert notifier.validate_channel_config("dingtalk", {}) == ["缺少 webhook"]
    for bad in ("ftp://example.com/x", "javascript:alert(1)"):
        errors = notifier.validate_channel_config("dingtalk", {"webhook": bad})
        assert errors == ["webhook 必须是 http(s) 地址"]


def test_validate_bark_invalid_server():
    cfg = {"key": "k", "server": "ftp://bad"}
    assert notifier.validate_channel_config("bark", cfg) == [
        "bark server 必须是 http(s) 地址"
    ]


def test_validate_telegram_requires_token_chat_id_and_api_base():
    ok = {"bot_token": "t", "chat_id": "c"}
    assert notifier.validate_channel_config("telegram", ok) == []
    assert notifier.validate_channel_config(
        "telegram", {"bot_token": "t", "chat_id": "c", "api_base": "https://api.telegram.org"}
    ) == []
    errors = notifier.validate_channel_config("telegram", {})
    assert "缺少 bot_token" in errors
    assert "缺少 chat_id" in errors
    bad = {"bot_token": "t", "chat_id": "c", "api_base": "ftp://bad"}
    assert notifier.validate_channel_config("telegram", bad) == [
        "api_base 必须是 http(s) 地址"
    ]


def test_validate_unknown_channel():
    assert notifier.validate_channel_config("wechat", {}) == ["未知渠道类型: wechat"]


def test_validate_config_accepts_none():
    assert notifier.validate_channel_config("bark", None) == notifier.validate_channel_config(
        "bark", {}
    )
    assert notifier.validate_channel_config("dingtalk", None) == ["缺少 webhook"]


def test_validate_channel_sample_dataset():
    cases = [
        ("bark", {"key": "device-key-001"}, []),
        ("bark", {"key": "", "server": "https://api.day.app"}, ["缺少 bark key"]),
        ("bark", {"key": "device-key-001", "server": "ftp://bad"}, ["bark server 必须是 http(s) 地址"]),
        ("dingtalk", {"webhook": "https://oapi.dingtalk.com/robot/send?access_token=sample"}, []),
        ("dingtalk", {"webhook": "ftp://oapi.dingtalk.com/x"}, ["webhook 必须是 http(s) 地址"]),
        ("telegram", {"bot_token": "123:bot", "chat_id": "chat-1"}, []),
        ("telegram", {"bot_token": "123:bot", "chat_id": ""}, ["缺少 chat_id"]),
        ("telegram", {"bot_token": "", "chat_id": "chat-1"}, ["缺少 bot_token"]),
        ("unknown", {"whatever": 1}, ["未知渠道类型: unknown"]),
    ]
    for ch_type, config, expected in cases:
        assert notifier.validate_channel_config(ch_type, config) == expected, ch_type


def test_dingtalk_sign_matches_hmac_sha256():
    secret = "SEC123"
    ts = "1720000000000"
    expected = urllib.parse.quote_plus(base64.b64encode(hmac.new(
        secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()))
    assert notifier._dingtalk_sign(secret, ts) == expected


def test_dingtalk_sign_is_url_safe():
    sign = notifier._dingtalk_sign("SECabc", "1720000000000")
    assert "+" not in sign
    assert "/" not in sign
    assert "=" not in sign
    decoded = base64.b64decode(urllib.parse.unquote_plus(sign))
    expected = hmac.new(
        b"SECabc", b"1720000000000\nSECabc", hashlib.sha256).digest()
    assert decoded == expected


def test_dingtalk_webhook_url_contains_timestamp_and_sign():
    secret = "SEC123"
    fixed_ms = 1720000000000
    cfg = {
        "webhook": "https://oapi.dingtalk.com/robot/send?access_token=x",
        "secret": secret,
    }
    captured = {}

    class FakeResponse:
        text = "ok"

        def json(self):
            return {"errcode": 0}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            return FakeResponse()

    with patch("app.notifier.time.time", return_value=fixed_ms / 1000), patch(
        "app.notifier.httpx.AsyncClient", FakeClient
    ):
        ok, detail = asyncio.run(
            notifier._send_dingtalk(cfg, "title", "body")
        )

    assert ok
    assert f"&timestamp={fixed_ms}" in captured["url"]
    assert f"&sign={notifier._dingtalk_sign(secret, str(fixed_ms))}" in captured["url"]


def test_notify_all_returns_per_channel_results():
    async def fake_send(ch_type, config, title, text):
        if ch_type == "bark":
            return True, "ok"
        return False, "missing"

    channels = [
        {"type": "bark", "config": {"key": "k"}},
        {"type": "telegram", "config": {}},
    ]
    with patch.object(notifier, "send_one", side_effect=fake_send):
        results = asyncio.run(notifier.notify_all(channels, "title", "body"))
    assert results == [
        {"type": "bark", "ok": True, "detail": "ok"},
        {"type": "telegram", "ok": False, "detail": "missing"},
    ]


def test_notify_all_continues_when_one_channel_fails():
    calls = 0

    async def fake_send(ch_type, config, title, text):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return True, "ok"

    channels = [
        {"type": "bark", "config": {}},
        {"type": "telegram", "config": {}},
    ]
    with patch.object(notifier, "send_one", side_effect=fake_send):
        results = asyncio.run(notifier.notify_all(channels, "title", "body"))
    assert len(results) == 2
    assert results[0]["ok"] is False
    assert "boom" in results[0]["detail"]
    assert results[1] == {"type": "telegram", "ok": True, "detail": "ok"}


def test_send_one_unknown_channel_no_network():
    ok, detail = asyncio.run(notifier.send_one("unknown", {}, "title", "body"))
    assert ok is False
    assert "未知渠道类型" in detail


def test_send_one_swallows_sender_exception():
    async def boom(cfg, title, text):
        raise ValueError("bad sender")

    with patch.dict(notifier._SENDERS, {"telegram": boom}):
        ok, detail = asyncio.run(notifier.send_one("telegram", {}, "title", "body"))
    assert ok is False
    assert "bad sender" in detail
