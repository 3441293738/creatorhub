import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.browser.douyin_im_pb import (
    _enc_ld,
    _enc_v,
    _first,
    _get_fields,
    _s,
    build_create_conversation_request,
    build_init_request,
    parse_create_conversation_response,
    parse_send_response,
)
from app.platforms.douyin.client import DouyinClient


@pytest.mark.parametrize("code", [0, "0"])
def test_json_im_success_without_ok_text(code):
    raw = json.dumps({"cmd": 100, "status_code": code, "error_desc": "",
                      "body": {"send_message_body": {}}}).encode()
    assert parse_send_response(raw) == {
        "ok": True, "msg": "OK", "cmd": 100, "error_code": 0,
    }


def test_json_init_response_remains_supported():
    assert parse_send_response(b'{"cmd":2043,"status_code":0}')["ok"]


def test_json_im_rejection_is_not_success():
    result = parse_send_response(
        b'{"cmd":100,"status_code":8,"error_desc":"rejected"}')
    assert result == {"ok": False, "msg": "rejected", "cmd": 100, "error_code": 8}


@pytest.mark.parametrize("raw", [
    b"", b"malformed-protobuf", b"<html>gateway</html>", b"{",
    _enc_ld(1, b"invalid-command") + _enc_ld(4, b"OK"),
    b"[]", b'{"status_code":0}', b'{"cmd":100}',
    b'{"cmd":0,"status_code":0}', b'{"cmd":true,"status_code":0}',
    b'{"cmd":100,"status_code":false}', b'{"cmd":100,"status_code":0.0}',
    b'{"cmd":"invalid","status_code":0}',
    b'{"cmd":100,"status_code":0,"error_desc":"rejected"}',
])
def test_invalid_or_ambiguous_response_is_not_success(raw):
    assert not parse_send_response(raw)["ok"]


def test_protobuf_im_success():
    raw = _enc_v(1, 100) + _enc_ld(4, b"OK")
    assert parse_send_response(raw) == {
        "ok": True, "msg": "OK", "cmd": 100, "error_code": 0,
    }


def test_protobuf_error_code_wins_over_ok_text():
    raw = _enc_v(1, 100) + _enc_v(3, 8) + _enc_ld(4, b"OK")
    assert not parse_send_response(raw)["ok"]


def test_client_send_rejects_success_envelope_for_wrong_command():
    client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
    response = SimpleNamespace(
        status_code=200,
        content=_enc_v(1, 2043) + _enc_v(3, 0) + _enc_ld(4, b"OK"),
    )
    session = AsyncMock()
    session.post.return_value = response
    session.close = AsyncMock()

    async def run():
        with patch("app.platforms.douyin.client.AsyncSession",
                   return_value=session):
            return await client.send_dm(
                "0:1:123456:987654", "42", "ticket", "hello")

    ok, error = asyncio.run(run())
    assert not ok
    assert error == "write_uncertain:invalid_response"


def test_create_request_matches_current_web_envelope_and_participant_order():
    raw = build_create_conversation_request(
        "123456", "987654", sequence_id=10007,
        user_agent="Mozilla/5.0 Chrome/152.0.0.0",
        locale="zh-CN", screen_width=1440, screen_height=900,
        referer="https://www.douyin.com/user/target")
    env = _get_fields(raw)
    assert sorted(env) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 14, 15, 18, 21, 22]
    assert _first(env, 1) == 609
    assert _first(env, 2) == 10007
    assert _s(_first(env, 3)) == "0.1.8"
    assert _first(env, 6) == 0
    assert 25 not in env

    body = _get_fields(_first(env, 8))
    create = _get_fields(_first(body, 609))
    assert _first(create, 1) == 1
    assert create[2] == [123456, 987654]

    headers = {}
    for value in env[15]:
        pair = _get_fields(value)
        headers[_s(_first(pair, 1))] = _s(_first(pair, 2))
    assert headers["user_agent"] == "Mozilla/5.0 Chrome/152.0.0.0"
    assert headers["screen_width"] == "1440"
    assert headers["screen_height"] == "900"
    assert headers["referer"] == "https://www.douyin.com/user/target"


def test_dynamic_envelope_matches_first_party_browser_capture_byte_for_byte():
    captured = (Path(__file__).parents[1]
                / "data/verification/douyin-api-compat/dm-init-request.pb")
    raw = build_init_request(
        1766414628284028,
        sequence_id=10002,
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "HeadlessChrome/152.0.0.0 Safari/537.36"),
        locale="zh-CN", screen_width=800, screen_height=600,
        referer="", timezone_name="Asia/Shanghai",
    )
    assert raw == captured.read_bytes()


def test_create_request_matches_first_party_browser_capture_byte_for_byte():
    captured = (Path(__file__).parents[1] / "data/verification"
                / "douyin-api-compat/dm-create-66790575681-firstparty.pb")
    raw = build_create_conversation_request(
        "3928976331901290", "510601289795662",
        sequence_id=10010,
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/152.0.0.0 Safari/537.36"),
        locale="zh-CN", screen_width=1440, screen_height=900,
        referer=("https://www.douyin.com/user/"
                 "MS4wLjABAAAASpjapUuFgjGhU_cw2ZCIXuMI9EwRrDzC4p28"
                 "gr1Rg7htuPCN8-Y1dxMd6lC61B-M"),
        timezone_name="Asia/Shanghai",
    )
    assert raw == captured.read_bytes()


def test_parse_create_conversation_response_extracts_send_identifiers():
    conversation = (
        _enc_ld(1, b"0:1:123456:987654")
        + _enc_v(2, 445566)
        + _enc_v(3, 1)
        + _enc_ld(4, b"ticket-value")
    )
    create = _enc_ld(1, conversation) + _enc_v(2, 0) + _enc_v(5, 0)
    raw = (_enc_v(1, 609) + _enc_v(3, 0) + _enc_ld(4, b"OK")
           + _enc_ld(6, _enc_ld(609, create)))
    assert parse_create_conversation_response(raw) == {
        "ok": True,
        "msg": "OK",
        "cmd": 609,
        "error_code": 0,
        "check_code": 0,
        "status": 0,
        "extra_info": "",
        "conversation": {
            "conv_id": "0:1:123456:987654",
            "conv_short_id": "445566",
            "conv_type": 1,
            "ticket": "ticket-value",
        },
    }


def test_parse_create_conversation_response_preserves_business_rejection():
    create = _enc_v(2, 17) + _enc_ld(3, "不允许私信".encode()) + _enc_v(5, 1)
    raw = (_enc_v(1, 609) + _enc_ld(4, b"OK")
           + _enc_ld(6, _enc_ld(609, create)))
    result = parse_create_conversation_response(raw)
    assert not result["ok"]
    assert result["error_code"] == 17
    assert result["msg"] == "不允许私信"
    assert result["conversation"] == {}


@pytest.mark.parametrize("raw, expected", [
    (b'{"cmd":100,"status_code":0,"error_desc":""}', (True, "")),
    (_enc_v(1, 100) + _enc_ld(4, b"OK"), (True, "")),
    (b'{"cmd":100}', (False, "write_uncertain:invalid_response")),
    (b'<html>gateway</html>', (False, "write_uncertain:invalid_response")),
    (b'{"cmd":100,"status_code":8,"error_desc":"rejected"}',
     (False, "api_rejected:code=8 rejected")),
])
def test_send_dm_decodes_real_response_without_parser_mock(raw, expected):
    session = AsyncMock()
    session.post.return_value = SimpleNamespace(status_code=200, content=raw)
    with patch("app.platforms.douyin.client.AsyncSession", return_value=session):
        client = DouyinClient("sid_tt=fixture", "Mozilla/5.0 Chrome/130.0.0.0")
        assert asyncio.run(client.send_dm("conversation", "42", "ticket", "hello")) == expected
    assert session.post.await_count == 1
    assert session.post.await_args.kwargs["headers"]["Accept"] == "application/x-protobuf"
    assert client.last_write_uncertain == expected[1].startswith("write_uncertain:")
