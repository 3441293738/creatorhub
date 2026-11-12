"""Persisted chat continuation uses bounded server-owned, scope-bound history."""
import asyncio
import hashlib
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlmodel import select

import app.db as db
from app.agent import runtime
from app.agent.config import SettingsUpdate, get_settings, save_channel, save_settings
from app.agent.conversations import context_key
from app.agent.providers import Turn
from app.models import AgentRun
from test_agent import client, finished, store
from test_project_optimizations import local_project


def configure(**kwargs):
    save_settings(SettingsUpdate(enabled=True, base_url="https://model.example/v1", model="fixture", **kwargs))


def capture_model(monkeypatch):
    calls = []
    class Model:
        def __init__(self, cfg, system, prompt):
            self.cfg = cfg; self.prompt = json.loads(prompt)
        async def complete(self, tools):
            calls.append({"model": self.cfg.model, **self.prompt})
            return Turn(text="Fixture answer SECRET-KEY")
    monkeypatch.setattr(runtime, "ModelSession", Model)
    return calls


def test_followup_preserves_context_and_idempotency_without_replaying_tools(local_project, monkeypatch):
    configure(api_key="SECRET-KEY")
    calls = capture_model(monkeypatch)
    async def scenario():
        first = await finished({"prompt": "第一问 SECRET-KEY", "platform": "douyin"})
        second_body = {"request_id": "followup-request-000001", "prompt": "具体怎么修复？", "platform": "douyin", "parent_run_id": first["id"]}
        second = await finished(second_body)
        assert second["conversation_id"] == first["id"] and second["parent_run_id"] == first["id"]
        assert second["conversation_title"] == first["conversation_title"]
        history = calls[1]["conversation_context"]["turns"]
        assert len(history) == 1 and history[0]["run_id"] == first["id"]
        assert "第一问" in history[0]["question"] and "Fixture answer" in history[0]["answer"]
        assert "SECRET-KEY" not in json.dumps(calls, ensure_ascii=False)
        assert "evidence" not in history[0] and "steps" not in history[0]
        assert len(calls[1]["initial_evidence"]) == 3
        assert (await finished(second_body))["id"] == second["id"] and len(calls) == 2
        async with client() as http:
            response = await http.get(f"/api/agent/runs/{second['id']}/conversation")
            assert response.status_code == 200 and "SECRET-KEY" not in response.text
            assert [row["id"] for row in response.json()["runs"]] == [first["id"], second["id"]]
            assert not response.json()["has_more"]
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["platform", "account", "mode", "channel", "destination", "credential"])
def test_history_cannot_cross_scope_or_destination(local_project, monkeypatch, change):
    configure(api_key="SECRET-KEY")
    calls = capture_model(monkeypatch)
    async def scenario():
        first = await finished({"prompt": "first", "platform": "douyin"})
        body = {"prompt": "followup", "platform": "douyin", "parent_run_id": first["id"]}
        if change == "platform": body["platform"] = "xhs"
        if change == "account": body["account_id"] = 999
        if change == "mode": body["mode"] = "workflow"
        if change == "channel":
            other = save_channel(SettingsUpdate(enabled=True, base_url="https://model.example/v1", model="fixture"), create=True)
            body["channel_id"] = other["channel"]["id"]
        if change == "destination":
            save_settings(SettingsUpdate(enabled=True, base_url="https://other.example/v1", model="fixture", api_key="NEW-KEY"))
        if change == "credential": configure(api_key="CHANGED-KEY")
        with pytest.raises(HTTPException) as error:
            runtime.start_run(runtime.RunInput(**body))
        assert error.value.status_code == 409 and "新建对话" in str(error.value.detail)
        assert len(calls) == 1
        with db.get_session() as session:
            assert len(session.exec(select(AgentRun)).all()) == 1
    asyncio.run(scenario())


def test_per_message_model_selection_keeps_channel_defaults_and_history(local_project, monkeypatch):
    configure()
    calls = capture_model(monkeypatch)
    async def scenario():
        first = await finished({"prompt": "first"})
        second = await finished({"prompt": "followup", "parent_run_id": first["id"], "model": "another-model"})
        assert second["model_name"] == "another-model" and len(calls[-1]["conversation_context"]["turns"]) == 1
    asyncio.run(scenario())
    assert get_settings().model == "fixture"


def test_context_window_is_bounded_and_latest_messages_are_retained(local_project, monkeypatch):
    configure()
    calls = capture_model(monkeypatch)
    key = context_key(runtime.RunInput(prompt="x"), get_settings(), "default")
    parent = None
    for i in range(12):
        run_id = f"history-record-{i:06d}"
        store(AgentRun(id=run_id, request_hash="x", conversation_id="history-record-000000", parent_run_id=parent,
                       context_key=key, status="completed", prompt=f"Question {i}", answer="a" * 9000))
        parent = run_id
    asyncio.run(finished({"prompt": "followup", "parent_run_id": parent}))
    context = calls[0]["conversation_context"]
    assert context["truncated"] and 1 <= len(context["turns"]) <= 8
    assert context["turns"][-1]["question"] == "Question 11"
    assert len(json.dumps(context["turns"], ensure_ascii=False)) < 20020
    assert all(len(item["answer"]) <= 6000 for item in context["turns"])


def test_legacy_record_is_viewable_but_not_implicitly_forwarded(local_project):
    configure()
    store(AgentRun(id="legacy-record-0000001", request_hash="x", prompt="Old question", answer="Old reply", status="completed"))
    result = runtime.get_conversation("legacy-record-0000001")
    assert result["runs"][0]["answer"] == "Old reply" and not result["runs"][0]["can_continue"]
    async def scenario():
        with pytest.raises(HTTPException) as error:
            runtime.start_run(runtime.RunInput(prompt="followup", parent_run_id="legacy-record-0000001"))
        assert error.value.status_code == 409
    asyncio.run(scenario())


def test_pending_parent_and_missing_parent_never_start_a_model_call(local_project):
    configure()
    key = context_key(runtime.RunInput(prompt="x"), get_settings(), "default")
    store(AgentRun(id="pending-record-0000001", request_hash="x", context_key=key, status="running"))
    async def scenario():
        for parent, status in [("pending-record-0000001", 409), ("missing-record-0000001", 404)]:
            with pytest.raises(HTTPException) as error:
                runtime.start_run(runtime.RunInput(prompt="followup", parent_run_id=parent))
            assert error.value.status_code == status
    asyncio.run(scenario())


def test_legacy_idempotency_hash_is_unchanged(local_project):
    body = runtime.RunInput(request_id="legacy-request-0000001", prompt="old")
    old_json = body.model_dump_json(exclude={"request_id", "parent_run_id", "channel_id", "model"})
    store(AgentRun(id=body.request_id, request_hash=hashlib.sha256(old_json.encode()).hexdigest(), status="completed", answer="Cached answer"))
    assert runtime.start_run(body)["answer"] == "Cached answer"


def test_chat_columns_upgrade_existing_table_without_rewriting_old_messages(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old-chat.db'}")
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE agentrun (id VARCHAR PRIMARY KEY, request_hash VARCHAR, prompt VARCHAR, answer VARCHAR, status VARCHAR)'))
        connection.execute(text("INSERT INTO agentrun VALUES ('old', 'hash', 'Original question', 'Original reply', 'completed')"))
    db._auto_migrate(engine)
    columns = {item["name"] for item in inspect(engine).get_columns("agentrun")}
    assert {"conversation_id", "parent_run_id", "context_key", "channel_id", "task_id", "usage_json", "reasoning_json"} <= columns
    with engine.connect() as connection:
        row = connection.execute(text('SELECT prompt,answer,conversation_id,parent_run_id FROM agentrun')).one()
        assert row == ("Original question", "Original reply", "", None)
        assert connection.execute(text('SELECT usage_json,reasoning_json FROM agentrun')).one() == ("[]", "[]")
    engine.dispose()
