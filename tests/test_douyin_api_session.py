"""Offline, account-scoped tests for the actual direct HTTP download path."""
import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

from curl_cffi.requests import Cookies
import pytest

import app.db as db
import app.main as main
import app.platforms.douyin.client as client_module
from app.browser.douyin_api_session import observe_douyin_api_session
from app.browser.identity import Identity
from app.engine.monitor import MonitorEngine
from app.models import AppSetting, AccountRiskState, DouyinAccount
from app.platforms.douyin.client import DouyinClient
from app.platforms.douyin.web_session import (
    DETAIL_PATH, context_binding, context_key, cookies_for_url,
    observed_detail_context, validated_detail_context,
)
from test_project_optimizations import local_project, store


UA = "Mozilla/5.0 Chrome/148.0.0.0"
WORK_ID = "1234567890123456789"
ORIGIN = "https://www-hj.douyin.com"


def state(token="TOKEN"):
    return {"cookies": [
        {"name": "sessionid", "value": "SESSION", "domain": ".douyin.com", "path": "/", "expires": -1},
        {"name": "msToken", "value": token, "domain": ".douyin.com", "path": "/", "expires": -1},
        {"name": "creator_only", "value": "PRIVATE", "domain": "creator.douyin.com", "path": "/"},
        {"name": "UIFID", "value": "ACCOUNT_DEVICE", "domain": ".douyin.com", "path": "/", "expires": -1},
    ], "origins": []}


def detail():
    return {"aweme_id": WORK_ID, "desc": "fixture", "author": {"nickname": "fixture"},
            "video": {"play_addr": {"url_list": ["https://media.example/fixture.mp4"]}}}


def recipe():
    return observed_detail_context(ORIGIN + DETAIL_PATH +
        "?aweme_id=OLD&aid=6383&version_code=290100&msToken=DO_NOT_SAVE&a_bogus=DO_NOT_REPLAY",
        {"Cookie": "DO_NOT_SAVE", "Authorization": "DO_NOT_SAVE", "sec-ch-ua-platform": '"Windows"'},
        ua=UA, fingerprint="ACTUAL_BROWSER_DIMENSIONS")


def response(payload=None, *, status=200, headers=None, cookies=None, body=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, cookies=cookies,
        content=body if body is not None else json.dumps(payload or {}).encode(),
        json=Mock(return_value=payload or {}))


def fake_http(monkeypatch, responses):
    session = SimpleNamespace(get=AsyncMock(side_effect=responses), close=AsyncMock())
    monkeypatch.setattr(client_module, "AsyncSession", lambda **_kwargs: session)
    sdk = AsyncMock(return_value="NEW_SDK_TOKEN")
    monkeypatch.setattr(client_module, "gen_real_ms_token", sdk)
    return session, sdk


def test_cookie_scope_matches_browser_domain_path_and_expiry():
    cookies = state()["cookies"] + [
        {"name": "evil", "value": "x", "domain": "evil-douyin.com", "path": "/"},
        {"name": "www_only", "value": "x", "domain": "www.douyin.com", "path": "/"},
        {"name": "expired", "value": "x", "domain": ".douyin.com", "path": "/", "expires": 999},
        {"name": "wrong_path", "value": "x", "domain": ".douyin.com", "path": "/aweme/other"},
        {"name": "path_cookie", "value": "x", "domain": ".douyin.com", "path": "/aweme"},
    ]
    selected = cookies_for_url(cookies, ORIGIN + DETAIL_PATH, now=1000)
    assert [c["name"] for c in selected] == ["path_cookie", "sessionid", "msToken", "UIFID"]
    assert not cookies_for_url(cookies, "https://douyin.com.example.invalid/", now=1000)
    assert not cookies_for_url(cookies, "https://mssdk.bytedance.com/", now=1000)


def test_observed_recipe_drops_auth_tokens_and_one_use_signature():
    encoded = json.dumps(recipe())
    assert "DO_NOT_SAVE" not in encoded and "DO_NOT_REPLAY" not in encoded
    assert "aweme_id" not in recipe()["params"]
    assert observed_detail_context("https://www-hj.douyin.com.evil/" + DETAIL_PATH, {}, ua=UA) == {}


@pytest.mark.parametrize("change", ["account", "session", "ua", "origin", "age", "proxy", "runtime"])
def test_observed_context_is_bound_to_account_session_environment_and_age(change):
    account = SimpleNamespace(id=13, profile_dir="fixture", proxy="", browser_runtime_id="one")
    current = state()
    value = recipe()
    value["binding"] = context_binding(account, current, UA)
    assert validated_detail_context(value, account, current, UA)
    ua = UA
    if change == "account": account.id = 14
    if change == "session": current["cookies"][0]["value"] = "OTHER_SESSION"
    if change == "ua": ua = "OTHER_UA"
    if change == "origin": value["origin"] = "https://example.invalid"
    if change == "age": value["captured_at"] -= 86400
    if change == "proxy": account.proxy = "http://proxy.example:8080"
    if change == "runtime": account.browser_runtime_id = "two"
    assert not validated_detail_context(value, account, current, ua)


def test_direct_api_reuses_valid_account_token_and_observed_route_without_browser(monkeypatch):
    http, sdk = fake_http(monkeypatch, [response({"status_code": 0, "aweme_detail": detail()})])
    signer = Mock(side_effect=lambda query, ua, **kw: query + "&a_bogus=FRESH_SIGNATURE")
    monkeypatch.setattr(client_module, "sign_url", signer)
    client = DouyinClient("legacy=IGNORED", UA, storage_state=json.dumps(state()), detail_context=recipe())
    assert asyncio.run(client.fetch_video_detail(WORK_ID)) == detail()
    sdk.assert_not_awaited()
    request = http.get.await_args
    parsed = urlsplit(request.args[0])
    query = parse_qs(parsed.query)
    assert parsed.netloc == "www-hj.douyin.com"
    assert query["aweme_id"] == [WORK_ID] and query["msToken"] == ["TOKEN"]
    assert query["version_code"] == ["290100"] and query["a_bogus"] == ["FRESH_SIGNATURE"]
    assert signer.call_args.kwargs["fp"] == "ACTUAL_BROWSER_DIMENSIONS"
    assert "creator_only" not in request.kwargs["headers"]["Cookie"]
    assert "IGNORED" not in request.kwargs["headers"]["Cookie"]
    assert request.kwargs["headers"]["sec-ch-ua-platform"] == '"Windows"'
    assert request.kwargs["allow_redirects"] is False
    assert client.last_response["business_code"] == 0
    assert client.last_response["observed_context_used"]
    diagnostic = json.dumps(client.last_response)
    assert all(secret not in diagnostic for secret in ("TOKEN", "SESSION", "PRIVATE", "FRESH_SIGNATURE"))


def test_next_request_uses_rotated_response_cookie_and_token(monkeypatch):
    cookies = Cookies()
    cookies.set("msToken", "ROTATED", domain=".douyin.com", path="/")
    cookies.set("ttwid", "REFRESHED", domain=".douyin.com", path="/")
    http, sdk = fake_http(monkeypatch, [response(cookies=cookies), response()])
    client = DouyinClient("", UA, storage_state=json.dumps(state()))

    async def run():
        async with client.session_scope():
            await client._get_json("/one", {})
            await client._get_json("/two", {})
    asyncio.run(run())
    sdk.assert_not_awaited()
    second = http.get.await_args_list[1]
    assert parse_qs(urlsplit(second.args[0]).query)["msToken"] == ["ROTATED"]
    assert "msToken=ROTATED" in second.kwargs["headers"]["Cookie"]
    assert "ttwid=REFRESHED" in second.kwargs["headers"]["Cookie"]
    assert any(c["value"] == "ROTATED" for c in client.export_storage_state()["cookies"])


def test_expired_token_does_not_leak_douyin_auth_cookies_to_sdk(monkeypatch):
    current = state()
    current["cookies"][1]["expires"] = 1
    http, sdk = fake_http(monkeypatch, [response()])
    client = DouyinClient("", UA, storage_state=json.dumps(current))
    asyncio.run(client._get_json("/one", {}))
    sdk.assert_awaited_once()
    assert sdk.await_args.kwargs["cookie"] == ""
    assert parse_qs(urlsplit(http.get.await_args.args[0]).query)["msToken"] == ["NEW_SDK_TOKEN"]


def test_header_token_rotation_updates_existing_cookie_without_changing_expiry(monkeypatch):
    current = state()
    fake_http(monkeypatch, [response(headers={"x-ms-token": "NEW_HEADER_TOKEN"})])
    client = DouyinClient("", UA, storage_state=json.dumps(current))
    asyncio.run(client._get_json("/one", {}))
    saved = client.export_storage_state()["cookies"][1]
    assert saved["value"] == "NEW_HEADER_TOKEN"
    assert saved["expires"] == -1 and saved["domain"] == ".douyin.com"
    assert client.last_response["token_source"] == "account_cookie"


def test_calibrated_request_does_not_reintroduce_obsolete_default_parameters():
    client = DouyinClient("", UA, storage_state=json.dumps(state()), detail_context=recipe())
    client._ms_token = "TOKEN"
    query = parse_qs(urlsplit(client._build_url(DETAIL_PATH, {"aweme_id": WORK_ID})).query)
    assert set(query) == {"aid", "version_code", "aweme_id", "msToken", "a_bogus",
                          "uifid", "timestamp", "x-secsdk-web-signature"}


def test_observed_headers_replace_defaults_case_insensitively():
    observed = recipe()
    observed["headers"]["accept-language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    client = DouyinClient("", UA, detail_context=observed)
    headers = client._headers(url=ORIGIN + DETAIL_PATH)
    matching = [value for name, value in headers.items() if name.lower() == "accept-language"]
    assert matching == ["zh-CN,zh;q=0.9,en;q=0.8"]


@pytest.mark.parametrize("attribute", ["Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"])
def test_response_cookie_deletion_is_not_resurrected_from_saved_state(monkeypatch, attribute):
    fake_http(monkeypatch, [response(headers={
        "set-cookie": f"msToken=; Domain=douyin.com; Path=/; {attribute}"})])
    client = DouyinClient("", UA, storage_state=json.dumps(state()))
    asyncio.run(client._get_json("/one", {}))
    assert not any(c["name"] == "msToken" for c in client.export_storage_state()["cookies"])


def test_response_cookie_replaces_equivalent_host_and_domain_key(monkeypatch):
    current = state()
    current["cookies"][1]["domain"] = "www.douyin.com"
    cookies = Cookies()
    cookies.set("msToken", "ROTATED", domain="www.douyin.com", path="/")
    fake_http(monkeypatch, [response(cookies=cookies)])
    client = DouyinClient("", UA, storage_state=json.dumps(current))
    asyncio.run(client._get_json("/one", {}))
    tokens = [c for c in client.export_storage_state()["cookies"] if c["name"] == "msToken"]
    assert len(tokens) == 1 and tokens[0]["value"] == "ROTATED"


@pytest.mark.parametrize("kind", ["403", "429", "html_captcha", "business_captcha"])
def test_direct_api_stops_on_denial_without_changing_hosts_or_retrying(monkeypatch, kind):
    r = response(status=int(kind)) if kind.isdigit() else response({"status_code": 8})
    if kind == "html_captcha":
        r = response(body="<html><title>验证码中间页</title></html>".encode())
        r.json.side_effect = ValueError("not json")
    http, _ = fake_http(monkeypatch, [r])
    client = DouyinClient("", UA, storage_state=json.dumps(state()), detail_context=recipe())
    assert asyncio.run(client.fetch_video_detail(WORK_ID)) is None
    assert client.last_error.startswith("risk:")
    http.get.assert_awaited_once()


def test_observer_waits_for_authoritative_self_profile_before_saving_recipe():
    ctx = SimpleNamespace(on=Mock(), storage_state=AsyncMock(return_value=state()))
    page = SimpleNamespace(evaluate=AsyncMock(return_value={"ua": UA, "fp": "BROWSER_FP"}))
    request = SimpleNamespace(all_headers=AsyncMock(return_value={"Cookie": "SECRET"}),
                              frame=SimpleNamespace(page=page))
    save = Mock(return_value=True)
    handler = observe_douyin_api_session(ctx, SimpleNamespace(account_id=13, ua=UA), save)
    work = SimpleNamespace(url=ORIGIN + DETAIL_PATH + f"?aweme_id={WORK_ID}", status=200,
        json=AsyncMock(return_value={"status_code": 0, "aweme_detail": detail()}), request=request)
    profile = SimpleNamespace(url="https://www.douyin.com/aweme/v1/web/user/profile/self/", status=200,
        json=AsyncMock(return_value={"status_code": 0, "user": {"uid": "SELF"}}))

    async def run():
        await handler(work)
        save.assert_not_called()
        await handler(profile)
        save.assert_called_once()
        assert save.call_args.args[3]["origin"] == ORIGIN
        profile.status = 401
        await handler(profile)
        await handler(work)
        save.assert_called_once()
    asyncio.run(run())


def test_observer_drains_in_flight_state_save_before_task_browser_close():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def storage_state():
            entered.set()
            await release.wait()
            return state()

        ctx = SimpleNamespace(on=Mock(), storage_state=storage_state)
        save = Mock(return_value=True)
        handler = observe_douyin_api_session(ctx, SimpleNamespace(account_id=13, ua=UA), save)
        profile = SimpleNamespace(url="https://www.douyin.com/aweme/v1/web/user/profile/self/", status=200,
            json=AsyncMock(return_value={"status_code": 0, "user": {"uid": "SELF"}}))
        task = asyncio.create_task(handler(profile))
        await asyncio.wait_for(entered.wait(), timeout=1)
        drain = asyncio.create_task(handler.drain_pending())
        await asyncio.sleep(0)
        assert not drain.done()
        release.set()
        await asyncio.wait_for(drain, timeout=1)
        save.assert_called_once()
        await task
        await asyncio.wait_for(handler.drain_pending(), timeout=1)

    asyncio.run(run())


def test_verified_browser_state_sync_does_not_clear_risk_or_accept_another_user(local_project):
    account_id = store(DouyinAccount(platform="douyin", uid="SELF", status="invalid",
        profile_dir="fixture", ua=UA, storage_state=json.dumps(state("OLD"))))
    cooldown = datetime.utcnow() + timedelta(hours=1)
    with db.get_session() as session:
        session.add(AccountRiskState(account_id=account_id, risk_level=1, cooldown_until=cooldown))
        session.commit()
        account = session.get(DouyinAccount, account_id)
        identity = Identity.from_account(account, "fixture", UA)
    assert not main._persist_douyin_read_session(identity, {"uid": "OTHER"}, state(), recipe())
    assert main._persist_douyin_read_session(identity, {"uid": "SELF"}, state(), recipe())
    with db.get_session() as session:
        account = session.get(DouyinAccount, account_id)
        assert account.status == "invalid"
        assert json.loads(account.storage_state) == state()
        assert session.get(AccountRiskState, account_id).cooldown_until == cooldown
        saved = session.get(AppSetting, context_key(account_id)).value
        assert validated_detail_context(saved, account, account.storage_state, UA)


def test_api_cookie_rotation_never_overwrites_newer_manual_session(local_project):
    account_id = store(DouyinAccount(platform="douyin", storage_state=json.dumps(state("NEW_LOGIN"))))
    main._persist_douyin_api_cookies(account_id, json.dumps(state("OLD")), state("ROTATED"))
    with db.get_session() as session:
        assert json.loads(session.get(DouyinAccount, account_id).storage_state) == state("NEW_LOGIN")


@pytest.mark.parametrize("http_status", [200, 403])
def test_share_api_only_runs_real_client_without_browser_or_generic_fallback(local_project, monkeypatch, http_status):
    cfg = local_project.cfg
    cfg.risk_control.enabled = False
    cfg.engine.quiet_hours_enabled = False
    cfg.engine.douyin_read_mode = "api"
    account_id = store(DouyinAccount(platform="douyin", uid="SELF", ua=UA,
        storage_state=json.dumps(state()), profile_dir="fixture"))
    with db.get_session() as session:
        account = session.get(DouyinAccount, account_id)
        value = recipe()
        value["binding"] = context_binding(account, account.storage_state, UA)
        session.add(AppSetting(key=context_key(account_id), value=json.dumps(value)))
        session.commit()
    monkeypatch.setattr(main, "engine", MonitorEngine(cfg, local_project.browser))
    monkeypatch.setattr(local_project.browser, "identity_for", lambda account:
        Identity.from_account(account, cfg.engine.profiles_dir, UA), raising=False)
    monkeypatch.setattr(main, "DouyinClient", DouyinClient)
    browser_read = AsyncMock(side_effect=AssertionError("API mode must not launch browser"))
    monkeypatch.setattr(main, "fetch_douyin_video_detail", browser_read)
    http, _ = fake_http(monkeypatch, [response({"status_code": 0, "aweme_detail": detail()}, status=http_status)])
    request = main._douyin_native_share(
        f"https://www.douyin.com/video/{WORK_ID}", account_id=account_id,
        output_root=local_project.root, quality="highest", should_download=False,
        save_metadata=False, save_thumbnail=False, proxy="", user_agent=UA)
    if http_status == 403:
        with pytest.raises(main.ShareDownloadError, match="HTTP 403") as caught:
            asyncio.run(request)
        assert caught.value.diagnostics["http_status"] == 403
        assert "TOKEN" not in json.dumps(caught.value.diagnostics)
    else:
        result = asyncio.run(request)
        assert result["ok"] and result["metadata"]["read_transport"] == "api"
        assert result["metadata"]["api_diagnostics"]["origin"] == ORIGIN
    browser_read.assert_not_awaited()
    http.get.assert_awaited_once()


# These platform contract tests exercise an authorized running client.
# License-denial behavior is covered by test_device_business_gate.py.
import pytest as _license_pytest

@_license_pytest.fixture(autouse=True)
def _authorized_platform_test(monkeypatch):
    monkeypatch.setattr("app.licensing.is_active", lambda: True)
