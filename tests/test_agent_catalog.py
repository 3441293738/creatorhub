"""Model discovery uses synthetic upstreams and isolated local settings only."""
import asyncio
import json

import httpx
import pytest
from sqlmodel import select

import app.db as db
from app.agent import catalog
from app.agent.catalog import CatalogError, discover_models, parse_models
from app.agent.config import ConnectionSettings, SettingsUpdate, get_settings, save_settings
from app.models import AgentRun
from test_agent import client
from test_project_optimizations import local_project


def mock_upstream(monkeypatch, handler):
    real = httpx.AsyncClient
    def factory(**kwargs):
        # Keep explicit ASGITransport for the local API; only outbound calls use
        # the synthetic upstream. Neither branch can access the public network.
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real(**kwargs)
    monkeypatch.setattr(catalog.httpx, "AsyncClient", factory)


def connection(**overrides):
    return ConnectionSettings(base_url="https://model.example/v1", api_key="FIXTURE-KEY", **overrides)


@pytest.mark.parametrize("provider", ["openai_compatible", "openai_responses", "anthropic"])
def test_catalog_provider_authentication_and_model_ids(monkeypatch, provider):
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "fixture-model", "display_name": "Fixture Model", "secret": "PRIVATE"}]})
    mock_upstream(monkeypatch, upstream)
    result = asyncio.run(discover_models(connection(provider=provider)))
    assert result["models"] == [{"id": "fixture-model", "name": "Fixture Model"}]
    request = requests[0]
    assert request.method == "GET" and request.url.path == "/v1/models"
    assert request.url.params.get("limit") == (None if provider == "openai_responses" else "1000")
    assert "FIXTURE-KEY" not in str(request.url) and not request.content
    if provider == "anthropic":
        assert request.headers["x-api-key"] == "FIXTURE-KEY"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in request.headers
    else:
        assert request.headers["Authorization"] == "Bearer FIXTURE-KEY"


@pytest.mark.parametrize("status", [400, 422])
def test_catalog_retries_only_rejected_limit_without_query(monkeypatch, status):
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(status, text="unsupported query") if request.url.query else httpx.Response(200, json={"data": []})
    mock_upstream(monkeypatch, upstream)
    assert asyncio.run(discover_models(connection()))["models"] == []
    assert len(requests) == 2 and not requests[1].url.query


@pytest.mark.parametrize("status", [401, 403, 404, 405, 429, 302, 500])
def test_catalog_errors_do_not_echo_body_follow_redirect_or_retry(monkeypatch, status):
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(status, text="FIXTURE-KEY PRIVATE-ERROR", headers={"Location": "https://another.example/models"})
    mock_upstream(monkeypatch, upstream)
    with pytest.raises(CatalogError) as error:
        asyncio.run(discover_models(connection()))
    assert "FIXTURE-KEY" not in str(error.value) and "PRIVATE-ERROR" not in str(error.value)
    assert len(requests) == 1
    if status == 401:
        assert "已携带 API Key" in str(error.value)


def test_catalog_local_model_may_omit_key(monkeypatch):
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"models": ["local-a", "local-b"]})
    mock_upstream(monkeypatch, upstream)
    result = asyncio.run(discover_models(ConnectionSettings(base_url="http://127.0.0.1:11434/v1")))
    assert result["count"] == 2 and "Authorization" not in requests[0].headers


def test_catalog_bounded_whitelisted_deduplicated_and_redacted():
    result = parse_models({"data": [
        {"id": "model-b", "display_name": "Label FIXTURE-KEY", "owned_by": "PRIVATE"},
        {"id": "model-a"}, {"id": "model-b"}, {"id": "FIXTURE-KEY"},
        {"id": "bad\nvalue"}, {"name": "missing-id"}, {"id": "x" * 161}, None,
    ], "has_more": True}, "FIXTURE-KEY")
    assert [row["id"] for row in result["models"]] == ["model-b", "model-a"]
    assert result["skipped"] == 5 and result["truncated"]
    assert "FIXTURE-KEY" not in json.dumps(result) and "PRIVATE" not in json.dumps(result)
    large = parse_models({"data": [{"id": str(i)} for i in range(1002)]})
    assert large["count"] == 1000 and large["truncated"]
    assert parse_models({"data": [{"id": "fixture", "display_name": " Model\nName\x00 "}]})["models"] == [{"id": "fixture", "name": "ModelName"}]


@pytest.mark.parametrize("content", [b"not JSON FIXTURE-KEY", b'{"data":null}', b"x" * 2_000_001],
                         ids=["invalid-json", "wrong-shape", "oversized"])
def test_catalog_rejects_malformed_or_oversized_responses(monkeypatch, content):
    mock_upstream(monkeypatch, lambda request: httpx.Response(200, content=content))
    with pytest.raises(CatalogError) as error:
        asyncio.run(discover_models(connection()))
    assert "FIXTURE-KEY" not in str(error.value)


def test_catalog_has_a_wall_clock_deadline(monkeypatch):
    async def upstream(request):
        await asyncio.sleep(10)
        return httpx.Response(200, json={"data": []})
    mock_upstream(monkeypatch, upstream)
    monkeypatch.setattr(catalog, "CATALOG_TIMEOUT", 0.01)
    with pytest.raises(CatalogError, match="超时"):
        asyncio.run(discover_models(connection()))


def test_catalog_api_accepts_unsaved_key_and_no_model_without_saving(local_project, monkeypatch):
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "discovered-model"}]})
    mock_upstream(monkeypatch, upstream)
    async def scenario():
        async with client() as http:
            response = await http.post("/api/agent/models", json={"base_url": "https://new.example/v1/", "api_key": "  NEW-FIXTURE-KEY  "})
            assert response.status_code == 200 and response.json()["count"] == 1
            assert response.headers["Cache-Control"] == "no-store"
            assert "NEW-FIXTURE-KEY" not in response.text
    asyncio.run(scenario())
    assert requests[0].headers["Authorization"] == "Bearer NEW-FIXTURE-KEY"
    saved = get_settings()
    assert not saved.enabled and not saved.base_url and not saved.api_key and not saved.model
    with db.get_session() as session:
        assert not session.exec(select(AgentRun)).all()


def test_catalog_api_preserves_saved_key_and_blocks_cross_origin_reuse(local_project, monkeypatch):
    save_settings(SettingsUpdate(base_url="https://model.example/v1", model="unchanged", api_key="SAVED-FIXTURE-KEY"))
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})
    mock_upstream(monkeypatch, upstream)
    async def scenario():
        async with client() as http:
            assert (await http.post("/api/agent/models", json={"base_url": "https://model.example/v1"})).status_code == 200
            assert requests[0].headers["Authorization"] == "Bearer SAVED-FIXTURE-KEY"
            for body in [{"base_url": "https://another.example/v1"},
                         {"base_url": "https://model.example/v1", "provider": "anthropic"}]:
                response = await http.post("/api/agent/models", json=body)
                assert response.status_code == 422 and "SAVED-FIXTURE-KEY" not in response.text
            assert len(requests) == 1
            response = await http.post("/api/agent/models", json={"base_url": "https://another.example/v1", "clear_api_key": True})
            assert response.status_code == 200 and "Authorization" not in requests[-1].headers
    asyncio.run(scenario())
    assert get_settings().api_key == "SAVED-FIXTURE-KEY" and get_settings().model == "unchanged"


def test_catalog_api_401_diagnoses_missing_or_saved_key_without_leaking(local_project, monkeypatch):
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(401, text="BAD-KEY SECRET-RESPONSE")
    mock_upstream(monkeypatch, upstream)
    async def scenario():
        async with client() as http:
            payload = {"base_url": "https://model.example/v1"}
            response = await http.post("/api/agent/models", json=payload)
            assert response.status_code == 502 and "未携带 API Key" in response.json()["detail"]
            save_settings(SettingsUpdate(**payload, api_key="BAD-KEY"))
            response = await http.post("/api/agent/models", json=payload)
            assert "已携带 API Key" in response.json()["detail"] and "401" in response.text
            assert "BAD-KEY" not in response.text and "SECRET-RESPONSE" not in response.text
    asyncio.run(scenario())
    assert len(requests) == 2


@pytest.mark.parametrize("payload", [{"api_key": {"value": "SECRET"}}, {"api_key": "SECRET\nInjected"},
                                    {"base_url": "https://model.example?token=SECRET"}, {"headers": {"Authorization": "SECRET"}}])
def test_catalog_validation_never_echoes_credentials(local_project, payload):
    async def scenario():
        async with client() as http:
            response = await http.post("/api/agent/models", json=payload)
            assert response.status_code == 422 and "SECRET" not in response.text
    asyncio.run(scenario())


def test_catalog_rejects_remote_or_cross_origin_requests_before_upstream(local_project, monkeypatch):
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})
    mock_upstream(monkeypatch, upstream)
    async def scenario():
        for peer, headers in [("192.0.2.8", {}), ("127.0.0.1", {"Origin": "https://another.example"})]:
            async with client(client=(peer, 1234)) as http:
                response = await http.post("/api/agent/models", headers=headers,
                                           json={"base_url": "https://model.example/v1"})
                assert response.status_code == 403
    asyncio.run(scenario())
    assert requests == []


def test_upstream_404_is_distinct_from_missing_local_route(local_project, monkeypatch):
    mock_upstream(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Not Found"}))
    async def scenario():
        async with client() as http:
            response = await http.post("/api/agent/models", json={"base_url": "https://model.example/v1"})
            assert response.status_code == 502
            assert "上游返回 404" in response.json()["detail"]
            assert "/v1" in response.json()["detail"]
    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["openai_compatible", "openai_responses", "anthropic"])
def test_bare_gateway_domain_works_without_forcing_v1(monkeypatch, provider):
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "fixture-gateway-model"}]})
    mock_upstream(monkeypatch, upstream)
    result = asyncio.run(discover_models(ConnectionSettings(provider=provider, base_url="https://gateway.example", api_key="FIXTURE-KEY")))
    assert result["count"] == 1 and not result["path_prefix_added"]
    assert len(requests) == 1 and requests[0].url.path == "/models"


@pytest.mark.parametrize("status", [404, 405])
def test_bare_domain_tries_v1_only_for_missing_catalog_path(monkeypatch, status):
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(status) if request.url.path == "/models" else httpx.Response(200, json={"data": [{"id": "fixture"}]})
    mock_upstream(monkeypatch, upstream)
    result = asyncio.run(discover_models(ConnectionSettings(provider="anthropic", base_url="https://gateway.example", api_key="FIXTURE-KEY")))
    assert result["path_prefix_added"] and result["count"] == 1
    assert [str(request.url) for request in requests] == ["https://gateway.example/models?limit=1000", "https://gateway.example/v1/models?limit=1000"]
    assert all(request.headers["x-api-key"] == "FIXTURE-KEY" for request in requests)


@pytest.mark.parametrize("status", [401, 403, 429, 302, 500])
def test_bare_domain_does_not_retry_auth_rate_redirect_or_server_errors(monkeypatch, status):
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://another.example/models"})
    mock_upstream(monkeypatch, upstream)
    with pytest.raises(CatalogError):
        asyncio.run(discover_models(ConnectionSettings(base_url="https://gateway.example", api_key="FIXTURE-KEY")))
    assert len(requests) == 1
