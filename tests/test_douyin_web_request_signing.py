"""Offline request-signing and precise rejection diagnostics."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from app.platforms.douyin import client as client_module
from app.platforms.douyin.client import DouyinClient
from app.platforms.douyin.detail import detail_http_error
from app.platforms.douyin.signing import mstoken
from app.platforms.douyin.signing.web_request import (
    DOUYIN_WEB_SIGNED_PATHS, requires_web_signature, sign_web_query,
    web_query_headers,
)
from app.platforms.douyin.web_session import DETAIL_PATH, observed_detail_context
from app.risk import RiskCategory, classify_platform_error


UA = "Mozilla/5.0 Chrome/148.0.0.0"
WORK = "1234567890123456789"


def account_state(device="DEVICE_ONE"):
    return {"cookies": [
        {"name": "UIFID", "value": device, "domain": ".douyin.com", "path": "/", "expires": -1},
        {"name": "sessionid", "value": "SESSION_SECRET", "domain": ".douyin.com", "path": "/", "expires": -1},
        {"name": "msToken", "value": "TOKEN_SECRET", "domain": ".douyin.com", "path": "/", "expires": -1},
        {"name": "s_v_web_id", "value": "VISITOR_ONE", "domain": ".douyin.com", "path": "/", "expires": -1},
    ]}


def http_fixture(monkeypatch, *, status=200, body=None, data=None):
    response = SimpleNamespace(status_code=status, content=body if body is not None else json.dumps(data or {}).encode(),
        headers={}, cookies=None, json=Mock(return_value=data or {}))
    session = SimpleNamespace(get=AsyncMock(return_value=response), close=AsyncMock())
    monkeypatch.setattr(client_module, "AsyncSession", lambda **_: session)
    return session


def test_canonical_bytes_match_component_encoding_and_preserve_order():
    query = "z=a+b&z=%E4%B8%AD%E6%96%87&bare&symbols=%21%27%28%29%2A%7E&plus=%2B%2F%3D"
    covered = "z=a%20b&z=%E4%B8%AD%E6%96%87&bare=&symbols=!'()*~&plus=%2B%2F%3D&uifid=DEVICE&timestamp=1700000000"
    expected = hashlib.md5(("DEVICE_1700000000_A96D855A08C0A9707F8BEF0D9A527E4E_" + covered).encode()).hexdigest()
    assert sign_web_query(query, "DEVICE", timestamp=1700000000) == covered + "&x-secsdk-web-signature=" + expected


def test_stale_signature_timestamp_and_duplicate_device_are_not_replayed():
    signed = sign_web_query("aweme_id=WORK&uifid=OLD&timestamp=1&uifid=OTHER&x-secsdk-web-signature=OLD",
                            "CURRENT", timestamp=1700000000)
    values = parse_qs(signed)
    assert values["uifid"] == ["CURRENT"] and values["timestamp"] == ["1700000000"]
    assert "OLD" not in signed and "OTHER" not in signed
    assert sign_web_query(signed, "CURRENT", timestamp=1700000000) == signed


@pytest.mark.parametrize("change", ["device", "work", "clock", "order"])
def test_seal_binds_identity_work_clock_and_parameter_order(change):
    query, device, stamp = "aweme_id=WORK&aid=6383", "DEVICE", 1700000000
    original = sign_web_query(query, device, timestamp=stamp)
    if change == "device": device = "DIFFERENT"
    if change == "work": query = "aweme_id=OTHER&aid=6383"
    if change == "clock": stamp += 1
    if change == "order": query = "aid=6383&aweme_id=WORK"
    assert parse_qs(original)["x-secsdk-web-signature"] != parse_qs(
        sign_web_query(query, device, timestamp=stamp))["x-secsdk-web-signature"]


@pytest.mark.parametrize("device", ["", "id\r\nX-Header: secret", "id;session=x", "id\x00"])
def test_invalid_identity_is_not_put_into_headers(device):
    with pytest.raises(ValueError):
        sign_web_query("aid=6383", device)
    assert web_query_headers("uifid=ID&timestamp=1&x-secsdk-web-signature=bad") == {}


def test_direct_detail_sends_native_seal_and_matching_device_without_browser(monkeypatch):
    payload = {"status_code": 0, "aweme_detail": {"aweme_id": WORK, "video": {"play_addr": {"url_list": ["https://media.example/fixture"]}}}}
    http = http_fixture(monkeypatch, data=payload)
    sdk = AsyncMock(side_effect=AssertionError("valid account token must be reused"))
    monkeypatch.setattr(client_module, "gen_real_ms_token", sdk)
    client = DouyinClient("", UA, storage_state=json.dumps(account_state()))
    assert asyncio.run(client.fetch_video_detail(WORK)) == payload["aweme_detail"]
    http.get.assert_awaited_once()
    call = http.get.await_args
    query = parse_qs(urlsplit(call.args[0]).query)
    assert query["uifid"] == [call.kwargs["headers"]["uifid"]] == ["DEVICE_ONE"]
    assert query["x-secsdk-web-signature"] == [call.kwargs["headers"]["x-secsdk-web-signature"]]
    assert query["timestamp"] == [call.kwargs["headers"]["x-secsdk-web-expire"]]
    assert query["verifyFp"] == query["fp"] == ["VISITOR_ONE"]
    assert client.last_response["web_signature_attached"]
    assert client.last_response["device_header_attached"]
    diagnostic = json.dumps(client.last_response)
    for secret in ("DEVICE_ONE", "SESSION_SECRET", "TOKEN_SECRET", query["x-secsdk-web-signature"][0]):
        assert secret not in diagnostic


def test_direct_post_list_uses_the_same_uifid_web_signature(monkeypatch):
    payload = {"status_code": 0, "aweme_list": [{"aweme_id": WORK}],
               "has_more": 0, "max_cursor": 0}
    http = http_fixture(monkeypatch, data=payload)
    monkeypatch.setattr(client_module, "gen_real_ms_token",
                        AsyncMock(side_effect=AssertionError("account token must be reused")))
    client = DouyinClient("", UA, storage_state=json.dumps(account_state()))
    page = asyncio.run(client.fetch_video_list("SEC_UID", 0, 5))
    assert [row["aweme_id"] for row in page["items"]] == [WORK]
    call = http.get.await_args
    query = parse_qs(urlsplit(call.args[0]).query)
    assert urlsplit(call.args[0]).path == "/aweme/v1/web/aweme/post/"
    assert query["uifid"] == [call.kwargs["headers"]["uifid"]] == ["DEVICE_ONE"]
    assert query["x-secsdk-web-signature"] == [call.kwargs["headers"]["x-secsdk-web-signature"]]
    assert client.last_response["web_signature_attached"]


def test_first_party_protected_path_registry_matches_current_web_sdk_scope():
    assert requires_web_signature("/aweme/v1/web/aweme/detail")
    assert requires_web_signature("/aweme/v1/web/aweme/post/")
    assert not requires_web_signature("/aweme/v1/web/comment/list/")
    assert len(DOUYIN_WEB_SIGNED_PATHS) == 14


@pytest.mark.parametrize("path", [DETAIL_PATH, "/aweme/v1/web/aweme/post/"])
@pytest.mark.parametrize("invalid", ["missing", "expired", "foreign_domain", "wrong_path", "header_injection"])
def test_invalid_or_missing_scoped_uifid_stops_before_http_and_sdk(monkeypatch, invalid, path):
    state = account_state()
    if invalid == "missing": state["cookies"].pop(0)
    elif invalid == "expired": state["cookies"][0]["expires"] = 1
    elif invalid == "foreign_domain": state["cookies"][0]["domain"] = "creator.douyin.com"
    elif invalid == "wrong_path": state["cookies"][0]["path"] = "/other"
    else: state["cookies"][0]["value"] = "bad\r\nvalue"
    http = http_fixture(monkeypatch)
    sdk = AsyncMock()
    monkeypatch.setattr(client_module, "gen_real_ms_token", sdk)
    client = DouyinClient("", UA, storage_state=json.dumps(state))
    if path == DETAIL_PATH:
        assert asyncio.run(client.fetch_video_detail(WORK)) is None
    else:
        assert asyncio.run(client.fetch_video_list("SEC_UID"))["items"] == []
    assert client.last_error == "api_signature:missing_uifid"
    assert client.last_response["request_sent"] is False
    http.get.assert_not_awaited()
    sdk.assert_not_awaited()


def test_rotated_identity_overrides_old_observed_query_and_does_not_leak_to_other_hosts():
    context = observed_detail_context("https://www.douyin.com" + DETAIL_PATH + "?uifid=OLD&aid=6383",
        {}, ua=UA)
    client = DouyinClient("", UA, storage_state=json.dumps(account_state()), detail_context=context)
    client._ms_token = "TOKEN"
    url = client._build_url(DETAIL_PATH, {"aweme_id": WORK})
    assert parse_qs(urlsplit(url).query)["uifid"] == ["DEVICE_ONE"]
    assert "uifid" not in client._headers(url=url.replace("www.douyin.com", "other.example"))


@pytest.mark.parametrize("reason,expected", [
    ("Uifid Not Found", "uifid_not_found"), ("Signature Not Found", "signature_not_found"),
    ("Sign Invalid", "sign_invalid"), ("Sign Expired", "sign_expired"),
])
def test_explicit_gateway_signature_rejection_is_not_account_risk(monkeypatch, reason, expected):
    http = http_fixture(monkeypatch, status=403, body=("Blocked by ArgusSecurityPlugin " + reason).encode())
    client = DouyinClient("", UA, storage_state=json.dumps(account_state()))
    assert asyncio.run(client.fetch_video_detail(WORK)) is None
    assert client.last_error == "api_signature:" + expected
    assert client.last_response["rejection_reason"] == expected
    assert classify_platform_error(client.last_error)[0] == RiskCategory.BUSINESS
    http.get.assert_awaited_once()


def test_unknown_403_is_still_a_stop_but_not_claimed_to_be_captcha():
    code, reason = detail_http_error(403, b"Forbidden")
    assert code == "risk:request_denied:http_403" and not reason
    assert classify_platform_error(code)[0] == RiskCategory.RISK
    assert detail_http_error(200, b"Blocked by ArgusSecurityPlugin Sign Invalid")[1] == ""
    assert detail_http_error(403, "<html><title>验证码中间页</title></html>".encode())[0] == "risk:verification_required:html"


@pytest.mark.parametrize("sdk_success", [True, False])
def test_token_source_distinguishes_issued_token_from_random_fallback(monkeypatch, sdk_success):
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.post.return_value = SimpleNamespace(status_code=200 if sdk_success else 403)
    session.cookies = SimpleNamespace(jar=[SimpleNamespace(name="msToken", value="ISSUED")])
    monkeypatch.setattr(mstoken, "AsyncSession", lambda **_: session)
    value = asyncio.run(mstoken.gen_real_ms_token(UA))
    assert isinstance(value, str)
    assert value.source == ("sdk" if sdk_success else "random_fallback")
    assert value == "ISSUED" if sdk_success else len(value) == 126


# These platform contract tests exercise an authorized running client.
# License-denial behavior is covered by test_device_business_gate.py.
import pytest as _license_pytest

@_license_pytest.fixture(autouse=True)
def _authorized_platform_test(monkeypatch):
    monkeypatch.setattr("app.licensing.is_active", lambda: True)
