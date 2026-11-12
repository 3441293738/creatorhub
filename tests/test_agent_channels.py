"""Multi-channel secrets, migration and selection use isolated local databases."""
import asyncio
import json

import httpx
import pytest

import app.db as db
from app.agent import catalog
from app.agent.config import (CHANNELS_KEY, SETTINGS_KEY, SettingsUpdate, activate_channel,
                              delete_channel, get_settings, list_channels, save_channel, save_settings)
from app.models import AppSetting
from test_agent import client
from test_project_optimizations import local_project


def config(**kwargs):
    return SettingsUpdate(base_url="https://model.example/v1", model="fixture", enabled=True, **kwargs)


def test_legacy_channel_is_read_without_migration_then_preserved_on_first_write(local_project):
    original = json.dumps({"base_url": "https://legacy.example", "model": "legacy", "enabled": True,
                           "api_key": "LEGACY-SECRET", "provider": "anthropic", "display_name": "旧渠道"})
    with db.get_session() as session:
        session.add(AppSetting(key=SETTINGS_KEY, value=original)); session.commit()
    public = list_channels()
    assert public["active_channel_id"] == "default" and len(public["channels"]) == 1
    assert "LEGACY-SECRET" not in json.dumps(public)
    with db.get_session() as session:
        assert session.get(AppSetting, CHANNELS_KEY) is None
        assert session.get(AppSetting, SETTINGS_KEY).value == original
    new = save_channel(config(display_name="新渠道", api_key="NEW-SECRET"), create=True)
    assert new["active_channel_id"] == "default" and len(new["channels"]) == 2
    assert get_settings().api_key == "LEGACY-SECRET" and get_settings().model == "legacy"
    assert get_settings(new["channel"]["id"]).api_key == "NEW-SECRET"
    assert "SECRET" not in json.dumps(new)


def test_edit_switch_and_delete_channels_keep_secrets_isolated(local_project):
    save_settings(config(api_key="FIRST-SECRET", display_name="一号"))
    second = save_channel(config(api_key="SECOND-SECRET", display_name="二号", mcp_enabled=True), create=True)["channel"]["id"]
    saved = save_channel(config(display_name="二号改名", tool_calling=False), second)
    assert get_settings(second).api_key == "SECOND-SECRET"
    assert get_settings().api_key == "FIRST-SECRET"
    assert saved["channel"]["api_key_set"] and "SECRET" not in json.dumps(saved)
    selected = activate_channel(second)
    assert selected["active_channel_id"] == second and not get_settings().tool_calling
    assert get_settings().api_key == "SECOND-SECRET"
    with db.get_session() as session:
        assert json.loads(session.get(AppSetting, SETTINGS_KEY).value)["api_key"] == "SECOND-SECRET"
    with pytest.raises(ValueError, match="先切换"):
        delete_channel(second)
    delete_channel("default")
    assert [item["id"] for item in list_channels()["channels"]] == [second]
    with pytest.raises(ValueError):
        get_settings("default")


def test_new_channel_never_inherits_active_secret_and_invalid_update_is_atomic(local_project):
    save_settings(config(api_key="PRIVATE"))
    second = save_channel(config(), create=True)["channel"]["id"]
    assert not get_settings(second).api_key
    before = list_channels()
    with pytest.raises(ValueError, match="重新填写"):
        save_channel(SettingsUpdate(base_url="https://another.example", model="x"), "default")
    assert list_channels() == before
    save_channel(config(clear_api_key=True), "default")
    assert not get_settings().api_key


def test_channels_limit_missing_and_incomplete_selection(local_project):
    empty = save_channel(SettingsUpdate(display_name="未配置"), create=True)["channel"]["id"]
    with pytest.raises(ValueError, match="尚未配置"):
        activate_channel(empty)
    with pytest.raises(ValueError, match="不存在"):
        activate_channel("unknown")
    for i in range(18):
        save_channel(config(display_name=f"渠道{i}"), create=True)
    with pytest.raises(ValueError, match="20"):
        save_channel(config(), create=True)
    assert len(list_channels()["channels"]) == 20


def test_channel_api_and_catalog_reuse_only_explicit_channel_key(local_project, monkeypatch):
    save_settings(config(api_key="ACTIVE-SECRET"))
    second = save_channel(config(api_key="OTHER-SECRET"), create=True)["channel"]["id"]
    sent = []
    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"data": [{"id": "fixture"}]})
    real = httpx.AsyncClient
    def factory(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real(**kwargs)
    monkeypatch.setattr(catalog.httpx, "AsyncClient", factory)
    async def scenario():
        async with client() as http:
            result = await http.get("/api/agent/channels")
            assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
            assert "SECRET" not in result.text
            payload = {"base_url": "https://model.example/v1", "channel_id": second}
            assert (await http.post("/api/agent/models", json=payload)).status_code == 200
            assert sent[-1].headers["authorization"] == "Bearer OTHER-SECRET"
            assert (await http.post("/api/agent/models", json={**payload, "channel_id": ""})).status_code == 200
            assert "authorization" not in sent[-1].headers
            assert (await http.post("/api/agent/models", json={**payload, "channel_id": "missing"})).status_code == 422
            assert len(sent) == 2
            assert (await http.delete("/api/agent/channels/default")).status_code == 409
            assert (await http.post(f"/api/agent/channels/{second}/activate")).status_code == 200
            assert (await http.delete("/api/agent/channels/default")).status_code == 200
            assert (await http.post("/api/agent/channels", json={"api_key": {"private": "SECRET"}})).status_code == 422
            response = await http.put(f"/api/agent/channels/{second}", json=config().model_dump())
            assert response.status_code == 200 and "SECRET" not in response.text
    asyncio.run(scenario())
    assert get_settings().api_key == "OTHER-SECRET"


def test_testing_inactive_channel_does_not_select_it(local_project, monkeypatch):
    from app.agent import api
    from app.agent.providers import Turn
    seen = []
    class Fake:
        def __init__(self, cfg, *_): seen.append(cfg.api_key)
        async def complete(self, tools):
            assert tools == []
            return Turn(text="OK")
    monkeypatch.setattr(api, "ModelSession", Fake)
    save_settings(config(api_key="FIRST"))
    second = save_channel(config(api_key="SECOND"), create=True)["channel"]["id"]
    async def scenario():
        async with client() as http:
            result = await http.post(f"/api/agent/channels/{second}/test")
            assert result.json()["ok"]
            assert (await http.post("/api/agent/channels/missing/test")).status_code == 404
    asyncio.run(scenario())
    assert seen == ["SECOND"] and get_settings().api_key == "FIRST"


def test_switch_does_not_change_an_inflight_analysis(local_project, monkeypatch):
    from app.agent import runtime
    from app.agent.providers import Turn
    save_settings(config(api_key="FIRST"))
    second = save_channel(config(api_key="SECOND"), create=True)["channel"]["id"]
    observed = []
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Model:
            def __init__(self, cfg, *_):
                self.key = cfg.api_key
            async def complete(self, _):
                observed.append(self.key)
                entered.set()
                await release.wait()
                return Turn(text="Fixture result")
        monkeypatch.setattr(runtime, "ModelSession", Model)
        first = runtime.start_run(runtime.RunInput(prompt="first"))
        handle = runtime.RUNNING[first["id"]]
        await asyncio.wait_for(entered.wait(), timeout=5)
        activate_channel(second)
        release.set()
        await handle
        other = runtime.start_run(runtime.RunInput(prompt="second"))
        await runtime.RUNNING[other["id"]]
        assert runtime.get_run(first["id"])["status"] == "completed"
    asyncio.run(scenario())
    assert observed == ["FIRST", "SECOND"]
