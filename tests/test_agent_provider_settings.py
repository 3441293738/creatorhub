"""Named provider form settings remain compatible with existing local configs."""
import asyncio
import json

import pytest

import app.db as db
from app.agent.config import SETTINGS_KEY, get_settings
from app.models import AppSetting
from test_agent import client
from test_project_optimizations import local_project


def test_named_provider_and_model_catalog_round_trip(local_project):
    async def scenario():
        async with client() as http:
            response = await http.put("/api/agent/settings", json={
                "display_name": "  我的模型服务  ", "base_url": "https://model.example/v1",
                "api_key": "FIXTURE-SECRET", "model": "private-id", "enabled": True,
                "models": [{"id": "  fixture-chat  ", "name": "  对话模型  "}, {"id": "fixture-reasoner"}],
            })
            assert response.status_code == 200
            saved = response.json()
            assert saved["display_name"] == "我的模型服务"
            assert saved["models"] == [{"id": "fixture-chat", "name": "对话模型"}, {"id": "fixture-reasoner", "name": ""}]
            # The catalog is a convenience, not an allowlist for model IDs.
            assert saved["model"] == "private-id" and saved["api_key_set"]
            assert "FIXTURE-SECRET" not in response.text and "api_key" not in saved
            reloaded = await http.get("/api/agent/settings")
            assert reloaded.json() == saved
            saved.pop("api_key_set")
            saved["models"] = []
            assert (await http.put("/api/agent/settings", json=saved)).status_code == 200
    asyncio.run(scenario())
    settings = get_settings()
    assert settings.api_key == "FIXTURE-SECRET" and settings.model == "private-id" and settings.models == []


def test_existing_settings_without_provider_name_or_catalog_still_load(local_project):
    original = json.dumps({"provider": "openai_compatible", "base_url": "https://old.example/v1",
                           "model": "legacy-id", "api_key": "LEGACY-KEY", "enabled": True})
    with db.get_session() as session:
        session.add(AppSetting(key=SETTINGS_KEY, value=original))
        session.commit()
    async def scenario():
        async with client() as http:
            response = await http.get("/api/agent/settings")
            assert response.status_code == 200 and "LEGACY-KEY" not in response.text
            saved = response.json()
            assert saved["display_name"] == "" and saved["models"] == []
            assert saved["model"] == "legacy-id" and saved["enabled"] and saved["api_key_set"]
    asyncio.run(scenario())
    with db.get_session() as session:
        assert session.get(AppSetting, SETTINGS_KEY).value == original


@pytest.mark.parametrize("payload", [
    {"display_name": "x" * 81}, {"display_name": "bad\nlabel"},
    {"models": [{"id": "same"}, {"id": " same "}]},
    {"models": [{"id": "   "}]}, {"models": [{"id": "x" * 161}]},
    {"models": [{"id": "bad\nvalue"}]}, {"models": [{"id": "fixture", "name": "x" * 201}]},
    {"models": [{"id": str(index)} for index in range(1001)]},
    {"models": [{"id": "fixture", "unexpected": "SECRET"}]},
], ids=["long-name", "control-in-name", "duplicate-id", "blank-id", "long-id", "control-in-id",
        "long-model-name", "catalog-cap", "extra-field"])
def test_provider_catalog_is_validated_without_echoing_input(local_project, payload):
    async def scenario():
        async with client() as http:
            response = await http.put("/api/agent/settings", json={**payload, "api_key": "PRIVATE-KEY"})
            assert response.status_code == 422
            assert "PRIVATE-KEY" not in response.text and "SECRET" not in response.text
    asyncio.run(scenario())
    assert not get_settings().api_key and not get_settings().enabled
