import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.browser.douyin_im_pb import _enc_ld, _enc_v, parse_send_response
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
