"""Offline regressions for provider export formats and proxy health checks."""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlmodel import select

import app.db as db
import app.main as main
from app.browser.manager import normalize_proxy
from app.browser.proxy import ProxyConfigError, ProxyPlan
from app.models import DouyinAccount, ProxyPool


FORMATS = [
    ("host_colon", "proxy.example:8080:USER:TOKEN"),
    ("host_csv", "proxy.example,8080,USER,TOKEN"),
    ("host_at", "proxy.example:8080@USER:TOKEN"),
    ("auth_colon", "USER:TOKEN:proxy.example:8080"),
    ("auth_at", "USER:TOKEN@proxy.example:8080"),
    ("host_hash", "proxy.example:8080##USER##TOKEN"),
]
CANONICAL = "socks5://USER:TOKEN@proxy.example:8080"


@pytest.mark.parametrize("input_format,raw", FORMATS)
@pytest.mark.parametrize("explicit_format", [False, True])
def test_all_provider_formats_share_one_canonical_proxy(raw, input_format, explicit_format):
    plan = ProxyPlan.parse(raw, default_scheme="socks5",
                           input_format=input_format if explicit_format else "auto")
    assert plan.normalized == CANONICAL
    assert plan.playwright() == {
        "server": "socks5://proxy.example:8080", "username": "USER", "password": "TOKEN"}
    assert ProxyPlan.parse(plan.normalized) == plan


@pytest.mark.parametrize("scheme", ["http", "https", "socks", "socks5", "socks5h"])
def test_explicit_scheme_overrides_default_and_socks_aliases_are_normalized(scheme):
    plan = ProxyPlan.parse(f"{scheme}://USER:TOKEN@proxy.example:8080", default_scheme="http")
    assert plan.scheme == ("socks5" if scheme.startswith("socks") else scheme)


@pytest.mark.parametrize("input_format,raw", FORMATS)
def test_scheme_prefixed_export_formats_are_supported(input_format, raw):
    assert ProxyPlan.parse("socks5://" + raw).normalized == CANONICAL


@pytest.mark.parametrize("raw,password", [
    ("USER:p%40ss%3A%23%2F%3F%25@proxy.example:8080", "p@ss:#/?%"),
    ("proxy.example:8080##USER##p@ss:#/?%40", "p@ss:#/?%40"),
    ('proxy.example,8080,USER,"p,ss@#/?%40"', "p,ss@#/?%40"),
    ("proxy.example:8080:USER:p:a:ss", "p:a:ss"),
    ("USER:p:a:ss:proxy.example:8080", "p:a:ss"),
    ("proxy.example:8080##USER##https://TOKEN", "https://TOKEN"),
])
def test_credentials_are_not_truncated_or_double_decoded(raw, password):
    plan = ProxyPlan.parse(raw)
    assert plan.password == password
    assert ProxyPlan.parse(plan.normalized) == plan
    assert password not in plan.redacted


@pytest.mark.parametrize("raw", [
    "[2001:db8::1]:8080:USER:TOKEN", "[2001:db8::1],8080,USER,TOKEN",
    "[2001:db8::1]:8080@USER:TOKEN", "USER:TOKEN:[2001:db8::1]:8080",
    "USER:TOKEN@[2001:db8::1]:8080", "[2001:db8::1]:8080##USER##TOKEN",
])
def test_all_formats_support_bracketed_ipv6(raw):
    plan = ProxyPlan.parse(raw)
    assert plan.normalized == "http://USER:TOKEN@[2001:db8::1]:8080"


def test_ambiguous_numeric_password_requires_format_instead_of_wrong_endpoint():
    raw = "proxy.example:8080@USER:1234"
    with pytest.raises(ProxyConfigError, match="歧义"):
        ProxyPlan.parse(raw)
    plan = ProxyPlan.parse(raw, input_format="host_at")
    assert (plan.host, plan.port, plan.username, plan.password) == (
        "proxy.example", 8080, "USER", "1234")
    # An explicit standard URL remains unambiguous, including numeric passwords.
    assert ProxyPlan.parse("http://USER:1234@proxy.example:8080") == plan


def test_ambiguous_colon_order_can_be_selected_explicitly():
    raw = "proxy.example:8080:USER:1234"
    with pytest.raises(ProxyConfigError, match="歧义"):
        ProxyPlan.parse(raw)
    assert ProxyPlan.parse(raw, input_format="host_colon").normalized == (
        "http://USER:1234@proxy.example:8080")
    assert ProxyPlan.parse(raw, input_format="auth_colon").normalized == (
        "http://proxy.example:8080@user:1234")


@pytest.mark.parametrize("raw", [
    "proxy.example:0:USER:TOKEN", "proxy.example:65536:USER:TOKEN",
    "proxy.example:abc@USER:TOKEN", "proxy.example,8080,USER",
    "http://USER:TOKEN@", "ftp://USER:TOKEN@proxy.example:8080",
    "http://USER:TOKEN@proxy.example:8080/path", "proxy.example:8080\nUSER:TOKEN",
    "USER:TOKEN%0A@proxy.example:8080",
])
def test_malformed_proxy_is_rejected_without_credentials(raw):
    with pytest.raises(ProxyConfigError) as caught:
        ProxyPlan.parse(raw)
    assert "TOKEN" not in str(caught.value)


def test_normalize_and_redaction_share_the_new_parser():
    for _, raw in FORMATS:
        assert normalize_proxy(raw) == CANONICAL.replace("socks5:", "http:")
        assert "TOKEN" not in main._mask_proxy(raw)
        assert "USER" not in main._mask_proxy(raw)


def mock_http(get):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.side_effect = get
    return patch("httpx.AsyncClient", return_value=client), client


def test_general_probe_never_uses_douyin_and_disables_environment_proxy():
    def get(url):
        assert "douyin" not in url
        return httpx.Response(200, json={"ip": "203.0.113.8"})
    factory, client = mock_http(get)
    with factory as constructor:
        ok, detail = asyncio.run(main._probe_proxy(CANONICAL))
    assert ok and "通用" in detail
    assert constructor.call_args.kwargs["proxy"] == CANONICAL
    assert constructor.call_args.kwargs["trust_env"] is False
    assert client.get.await_count == 1


@pytest.mark.parametrize("first", [
    httpx.ProxyError("General SOCKS server failure"), httpx.ReadTimeout("timeout"),
    httpx.Response(403), httpx.Response(429), httpx.Response(503),
    httpx.Response(200, text="<html>proxy error</html>"),
    httpx.Response(200, json={"ip": "not-an-ip"}), httpx.Response(200, json=[]),
])
def test_general_probe_tries_another_ip_service(first):
    factory, client = mock_http([first, httpx.Response(200, json={"ip": "2001:db8::1"})])
    with factory:
        ok, _ = asyncio.run(main._probe_proxy(CANONICAL))
    assert ok
    assert client.get.await_count == 2
    assert client.get.await_args_list[0] != client.get.await_args_list[1]


@pytest.mark.parametrize("response", [
    httpx.Response(407), httpx.ProxyError("407 Proxy Authentication Required"),
    httpx.ProxyError("Invalid username/password"),
])
def test_authentication_failure_is_not_treated_as_healthy(response):
    factory, client = mock_http([response])
    with factory:
        ok, detail = asyncio.run(main._probe_proxy(CANONICAL))
    assert not ok
    assert main._proxy_status_from_detail(ok, detail) == "auth_error"
    assert client.get.await_count == 1


def test_failed_probes_keep_diagnostics_but_never_echo_credentials():
    factory, client = mock_http(httpx.ProxyError(f"failed {CANONICAL} USER TOKEN"))
    with factory:
        ok, detail = asyncio.run(main._probe_proxy(CANONICAL))
    assert not ok
    assert "USER" not in detail and "TOKEN" not in detail
    assert "ProxyError" in detail
    assert client.get.await_count == 2


@pytest.mark.parametrize("platform,host", [
    ("douyin", "www.douyin.com"), ("tiktok", "www.tiktok.com"),
    ("xhs", "www.xiaohongshu.com"), ("kuaishou", "www.kuaishou.com"),
    ("shipinhao", "channels.weixin.qq.com"),
])
def test_platform_probe_still_tests_only_the_requested_platform(platform, host):
    factory, client = mock_http(httpx.ProxyError("General SOCKS server failure"))
    with factory:
        ok, detail = asyncio.run(main._probe_proxy(CANONICAL, platform))
    assert not ok and host in detail
    client.get.assert_awaited_once_with(f"https://{host}/")


def test_empty_proxy_does_not_silently_probe_direct_connection():
    with patch("httpx.AsyncClient") as client:
        assert asyncio.run(main._probe_proxy(""))[0] is False
    client.assert_not_called()


@pytest.fixture
def proxy_db(tmp_path, monkeypatch):
    old_engine = db._engine
    db.init_db(str(tmp_path / "proxies.db"))
    monkeypatch.setattr(main, "browser", None)
    try:
        yield
    finally:
        db._engine.dispose()
        db._engine = old_engine


@pytest.mark.parametrize("input_format,raw", FORMATS)
def test_add_proxy_and_account_setting_accept_all_formats(proxy_db, input_format, raw):
    result = asyncio.run(main.add_proxy(main.PoolProxyIn(url=raw, default_scheme="socks5")))
    assert result["url_full"] == CANONICAL
    with db.get_session() as session:
        account = DouyinAccount(nickname="fixture", platform="tiktok")
        session.add(account)
        session.commit()
        account_id = account.id
    asyncio.run(main.set_account_proxy(account_id, main.ProxyIn(proxy=raw, default_scheme="socks5")))
    with db.get_session() as session:
        assert session.get(DouyinAccount, account_id).proxy == CANONICAL


def test_bulk_import_deduplicates_formats_without_eating_csv_host(proxy_db):
    text = "# comment\n" + "\n".join(raw for _, raw in FORMATS)
    text += "\nUS,other.example,8080,USER,TOKEN\nJP | third.example:8080##USER##TOKEN"
    text += "\ninvalid,TOKEN\n"
    result = asyncio.run(main.import_proxies(main.ProxyImportIn(text=text, default_scheme="socks5")))
    assert (result["added"], result["skipped"], result["invalid"]) == (3, 5, 1)
    assert "TOKEN" not in str(result["invalid_samples"])
    with db.get_session() as session:
        rows = session.exec(select(ProxyPool).order_by(ProxyPool.id)).all()
        assert rows[0].url == CANONICAL and rows[0].label == ""
        assert rows[1].label == "US" and "other.example" in rows[1].url
        assert rows[2].label == "JP" and "third.example" in rows[2].url


@pytest.mark.parametrize("input_format,raw", FORMATS)
def test_auto_detect_uses_shared_parser_and_preserves_credentials(input_format, raw):
    probe = AsyncMock(return_value=(True, "通用连通性正常"))
    with patch.object(main, "_probe_proxy", probe), patch.object(main, "_proxy_geo", AsyncMock(return_value=None)):
        result = asyncio.run(main.detect_proxy(main.ProxyDetectIn(url=raw, default_scheme="socks5")))
    assert result["ok"] and result["recommend"] == CANONICAL
    probe.assert_awaited_once_with(CANONICAL, timeout=8)


def test_invalid_auto_detect_returns_a_validation_error_without_network():
    with patch.object(main, "_probe_proxy", AsyncMock()) as probe:
        result = asyncio.run(main._detect_proxy("proxy.example:BAD@USER:TOKEN"))
    assert not result["ok"] and "TOKEN" not in result["error"]
    probe.assert_not_awaited()


def test_pool_health_is_independent_from_account_platform_health(proxy_db):
    with db.get_session() as session:
        pool = ProxyPool(url=CANONICAL, status="bad")
        account = DouyinAccount(nickname="fixture", platform="douyin", proxy=CANONICAL, identity_mode="legacy")
        session.add_all([pool, account])
        session.commit()
        pid, aid = pool.id, account.id

    def get(url):
        if "douyin" in url:
            raise httpx.ProxyError("General SOCKS server failure")
        return httpx.Response(200, json={"ip": "203.0.113.8"})
    factory, _ = mock_http(get)
    geo = {"ip": "203.0.113.8", "country": "US"}
    with factory, patch.object(main, "_proxy_geo", AsyncMock(return_value=geo)):
        pool_result = asyncio.run(main.test_proxy_entry(pid))
        account_result = asyncio.run(main.test_account_proxy(aid))
    assert pool_result["ok"] and not account_result["ok"]
    with db.get_session() as session:
        assert session.get(ProxyPool, pid).status == "ok"
        assert session.get(ProxyPool, pid).country == "US"
        assert session.get(DouyinAccount, aid).proxy_status == "bad"


def test_geo_metadata_failure_does_not_mark_a_working_proxy_bad(proxy_db):
    result = asyncio.run(main.add_proxy(main.PoolProxyIn(url=CANONICAL)))
    with patch.object(main, "_probe_proxy", AsyncMock(return_value=(True, "通用连通性正常"))), \
            patch.object(main, "_proxy_geo", AsyncMock(return_value=None)):
        tested = asyncio.run(main.test_proxy_entry(result["id"]))
    assert tested["ok"] and tested["geo"] is None


def test_selected_format_disambiguates_add_import_and_detect(proxy_db):
    options = {"default_scheme": "socks5", "input_format": "host_at"}
    raw = "proxy.example:8080@USER:1234"
    expected = "socks5://USER:1234@proxy.example:8080"
    result = asyncio.run(main.add_proxy(main.PoolProxyIn(url=raw, **options)))
    assert result["url_full"] == expected
    imported = asyncio.run(main.import_proxies(main.ProxyImportIn(text=raw, **options)))
    assert imported["skipped"] == 1
    probe = AsyncMock(return_value=(True, "通用连通性正常"))
    with patch.object(main, "_probe_proxy", probe), patch.object(main, "_proxy_geo", AsyncMock(return_value=None)):
        detected = asyncio.run(main.detect_proxy(main.ProxyDetectIn(url=raw, **options)))
    assert detected["recommend"] == expected


def test_api_validation_and_deduplication_use_the_canonical_url(proxy_db):
    async def scenario():
        transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post("/api/proxies", json={
                "url": FORMATS[0][1], "default_scheme": "socks5"})
            assert response.status_code == 200
            pid = response.json()["id"]
            response = await client.post("/api/proxies", json={"url": CANONICAL})
            assert response.status_code == 409
            response = await client.put(f"/api/proxies/{pid}", json={
                "url": "other.example:1080##USER##TOKEN", "default_scheme": "socks5"})
            assert response.status_code == 200
            assert response.json()["url_full"] == "socks5://USER:TOKEN@other.example:1080"
            response = await client.post("/api/proxies/detect", json={"url": "proxy.example:BAD@USER:TOKEN"})
            assert response.status_code == 200 and not response.json()["ok"]
            assert "TOKEN" not in response.text
            response = await client.post("/api/proxies", json={
                "url": CANONICAL, "input_format": "unsupported"})
            assert response.status_code == 422
    asyncio.run(scenario())
