"""Offline AI/MCP acceptance: temporary DBs, synthetic evidence, mocked models."""
import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import select

import app.db as db
import app.main as main
from app.agent import providers, runtime
from app.agent.config import AgentSettings, SettingsUpdate, get_settings, save_settings
from app.agent.mcp_server import ALLOWED_TOOLS, validate_base
from app.agent.privacy import redact
from app.agent.providers import ModelError, ModelSession, ToolCall, Turn
from app.agent.tools import read_tool
from app.agent.workflows import apply_plan, save_plan, validate_plan
from app.models import (AccountOperationLog, AccountStatSnapshot, AccountWork, AgentRun,
                        AgentWorkflow, DouyinAccount, KeywordCollectionJob, MonitorTarget,
                        PublishTask, RiskEvent, TaskSubmission)
from test_project_optimizations import local_project, store
from agent_stream_fixture import response as stream_response


def client(**kwargs):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, **kwargs), base_url="http://127.0.0.1")


def enabled(**kwargs):
    save_settings(SettingsUpdate(enabled=True, base_url="https://model.example/v1", model="fixture", **kwargs))


def account(platform="douyin", **kwargs):
    return store(DouyinAccount(platform=platform, nickname="Fixture", status="active", storage_state='{"cookie":"PRIVATE"}', **kwargs))


def plan_args(acc, **step):
    return {"title": "本地采集计划", "steps": [{"kind": "create_collection", "platform": "douyin",
            "account_id": acc, "keywords": ["摄影"], **step}]}


class FakeModel:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.results = []
        self.complete_count = 0

    async def complete(self, tools):
        self.complete_count += 1
        return next(self.turns)

    def add_results(self, results):
        self.results.extend(results)


def install_model(monkeypatch, turns):
    fake = FakeModel(turns)
    monkeypatch.setattr(runtime, "ModelSession", lambda *_: fake)
    return fake


async def finished(body):
    result = runtime.start_run(runtime.RunInput(**body))
    task = runtime.RUNNING.get(result["id"])
    if task:
        await task
    return runtime.get_run(result["id"])


def test_settings_default_secret_and_old_settings_isolation(local_project):
    assert not get_settings().enabled and not get_settings().mcp_enabled
    async def scenario():
        async with client() as http:
            config = {"base_url": "https://model.example/v1", "model": "fixture", "api_key": "PRIVATE-KEY", "enabled": True}
            result = await http.put("/api/agent/settings", json=config)
            assert result.status_code == 200 and result.json()["api_key_set"]
            assert "PRIVATE-KEY" not in result.text and "api_key" not in result.json()
            assert "PRIVATE-KEY" not in (await http.get("/api/agent/settings")).text
            saved = result.json(); saved.pop("api_key_set")
            assert (await http.put("/api/agent/settings", json=saved)).status_code == 200
            assert get_settings().api_key == "PRIVATE-KEY"
            saved["base_url"] = "https://another.example/v1"
            assert (await http.put("/api/agent/settings", json=saved)).status_code == 422
            saved["clear_api_key"] = True
            assert (await http.put("/api/agent/settings", json=saved)).status_code == 200
            assert get_settings().api_key == ""
            bad = await http.put("/api/agent/settings", json={"api_key": {"value": "SECRET-IN-ERROR"}})
            assert bad.status_code == 422 and "SECRET-IN-ERROR" not in bad.text
            assert (await http.get("/api/settings")).json()["ai_enabled"] is False
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["file:///secret", "https://user:pass@host/v1", "https://host/v1?key=x", "https://host/v1#key", "https://host:bad/v1"])
def test_provider_endpoint_validation(url):
    with pytest.raises(ValidationError):
        AgentSettings(base_url=url)


def test_read_tools_are_allowlisted_scoped_and_redacted(local_project):
    acc = account(cookie="COOKIE-SECRET", proxy="http://user:pass@proxy.test:8080")
    account("xhs")
    job = store(KeywordCollectionJob(account_id=acc, error="登录态已失效 cookie=RAW-SECRET https://host/error?token=URL-SECRET"))
    store(AccountOperationLog(account_id=acc, task_type="collection", task_id=str(job), detail="authorization=BEARER-SECRET", duration_ms=120))
    store(AccountOperationLog(account_id=acc, task_type="publish", task_id=str(job), detail="WRONG-TASK"))
    store(RiskEvent(account_id=acc, signal="auth_expired", outcome="auth"))
    result = read_tool("get_task", {"task_type": "collections", "task_id": job}, platform="douyin")
    text = json.dumps(result)
    for value in ("COOKIE-SECRET", "PRIVATE", "RAW-SECRET", "URL-SECRET", "BEARER-SECRET", "WRONG-TASK", "storage_state"):
        assert value not in text
    assert result["triage"]["classification"] == "auth"
    assert result["account_risk_events"]["events"][0]["signal"] == "auth_expired"
    assert len(read_tool("list_accounts", {}, platform="douyin")["accounts"]) == 1
    for name, args in [("execute_sql", {}), ("propose_workflow", {}), ("list_accounts", {"platform": "xhs"})]:
        with pytest.raises(ValueError):
            read_tool(name, args, platform="douyin")


def test_metrics_window_status_and_missing_measurements(local_project):
    acc = account()
    store(PublishTask(platform="douyin", account_id=acc, status="failed"))
    store(PublishTask(platform="douyin", account_id=acc, status="done", created_at=datetime.utcnow() - timedelta(days=20)))
    store(AccountStatSnapshot(account_id=acc, date=datetime.utcnow().date().isoformat(), follower_count=4))
    store(AccountWork(account_id=acc, like_count=10, play_count=0))
    result = read_tool("get_metrics", {"days": 7}, platform="douyin", account_id=acc)
    assert result["task_status_counts"]["publishes"] == {"failed": 1}
    assert result["duration"]["mean_ms"] is None
    assert result["window"]["timezone"] == "UTC" and result["limitations"]
    assert result["account_snapshots"][0]["follower_count"] == 4


def test_mcp_and_remote_boundary_default_closed(local_project):
    async def scenario():
        async with client() as http:
            assert (await http.get("/api/agent/tools")).status_code == 403
            assert (await http.post("/api/agent/runs", json={"prompt": "hello"})).status_code == 409
            save_settings(SettingsUpdate(mcp_enabled=True))
            result = await http.get("/api/agent/tools")
            assert {tool["name"] for tool in result.json()} == ALLOWED_TOOLS
            assert (await http.post("/api/agent/tools/list_accounts", json={})).status_code == 200
            assert (await http.post("/api/agent/tools/propose_workflow", json={})).status_code == 422
            assert (await http.post("/api/agent/tools/list_accounts", json={"sql": "drop table"})).status_code == 422
            assert (await http.post("/api/agent/tools/list_accounts", json={}, headers={"Origin": "https://evil.example"})).status_code == 403
        async with client(client=("192.0.2.1", 1234)) as http:
            assert (await http.get("/api/agent/settings")).status_code == 403
    asyncio.run(scenario())


@pytest.mark.parametrize("base", ["http://evil.example", "http://127.0.0.1@evil.example", "http://127.0.0.1/path", "http://localhost/?key=x", "file:///tmp", "http://localhost:bad"])
def test_mcp_bridge_never_targets_remote_servers(base):
    with pytest.raises(ValueError):
        validate_base(base)


@pytest.mark.parametrize("provider", ["openai_compatible", "openai_responses", "anthropic"])
def test_provider_tool_roundtrip(provider, monkeypatch):
    cfg = AgentSettings(provider=provider, base_url="https://model.example/v1", model="fixture", api_key="PRIVATE")
    session = ModelSession(cfg, "system", "question")
    if provider == "openai_compatible":
        payload = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "list_accounts", "arguments": "{}"}}]}}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}
    elif provider == "openai_responses":
        payload = {"output": [{"type": "function_call", "call_id": "c1", "name": "list_accounts", "arguments": "{}"}], "usage": {"input_tokens": 4, "output_tokens": 2}}
    else:
        payload = {"content": [{"type": "tool_use", "id": "c1", "name": "list_accounts", "input": {}}], "usage": {"input_tokens": 4, "output_tokens": 2}}
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return stream_response(provider, payload)
        if provider == "anthropic":
            answer = {"content": [{"type": "text", "text": "fixture answer"}], "stop_reason": "end_turn"}
        elif provider == "openai_responses":
            answer = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "fixture answer"}]}], "status": "completed"}
        else:
            answer = {"choices": [{"message": {"content": "fixture answer"}, "finish_reason": "stop"}]}
        return stream_response(provider, answer)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    turn = asyncio.run(session.complete([{ "name": "list_accounts", "description": "read", "inputSchema": {"type": "object", "properties": {}}}]))
    assert (turn.input_tokens, turn.output_tokens) == (4, 2)
    assert turn.calls[0].name == "list_accounts"
    session.add_results([(turn.calls[0], {"ok": True})])
    _, headers, body = session.request([])
    assert "PRIVATE" not in json.dumps(body)
    assert "c1" in json.dumps(body)
    assert requests[0].headers["x-api-key" if provider == "anthropic" else "Authorization"].endswith("PRIVATE")
    final = asyncio.run(session.complete([]))
    assert final.text == "fixture answer" and not final.calls
    sent = json.loads(requests[1].content)
    if provider == "openai_compatible":
        assert sent["messages"][-1]["role"] == "tool"
    elif provider == "openai_responses":
        assert sent["input"][-1]["type"] == "function_call_output"
        assert sent["store"] is False and "reasoning.encrypted_content" in sent["include"]
    else:
        assert sent["messages"][-1]["content"][0]["type"] == "tool_result"


@pytest.mark.parametrize("provider,payload", [
    ("openai_compatible", {"choices": [{"finish_reason": "length"}]}),
    ("openai_responses", {"status": "incomplete"}),
    ("anthropic", {"stop_reason": "max_tokens"}),
])
def test_model_truncated_output_is_not_reported_as_success(provider, payload):
    with pytest.raises(ModelError):
        ModelSession(AgentSettings(provider=provider), "system", "prompt").parse(payload)


@pytest.mark.parametrize("status,content", [(401, b"PRIVATE KEY"), (302, b"PRIVATE"), (200, b"not json PRIVATE")])
def test_model_errors_do_not_echo_credentials(status, content, monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, content=content)), **kw))
    with pytest.raises(ModelError) as error:
        asyncio.run(ModelSession(AgentSettings(base_url="https://model.example", model="x", api_key="PRIVATE"), "s", "p").complete([]))
    assert "PRIVATE" not in str(error.value)


def test_local_model_no_key_and_explicit_token_parameter():
    cfg = AgentSettings(base_url="http://127.0.0.1:11434/v1", model="local", output_token_parameter="max_completion_tokens")
    url, headers, body = ModelSession(cfg, "s", "p").request([])
    assert url.endswith("/v1/chat/completions") and "Authorization" not in headers
    assert body["max_completion_tokens"] == 2048 and "max_tokens" not in body


def test_runtime_diagnosis_persists_evidence_and_native_tools(local_project, monkeypatch):
    enabled(api_key="MODEL-SECRET")
    acc = account()
    job = store(KeywordCollectionJob(account_id=acc, error="登录态已失效"))
    fake = install_model(monkeypatch, [Turn(calls=[ToolCall("c1", "get_metrics", {"days": 1})], input_tokens=8),
                                     Turn(text="依据 e2：检查登录状态 MODEL-SECRET", output_tokens=9)])
    body = {"request_id": "diagnosis-request-0001", "prompt": "分析失败", "platform": "douyin", "mode": "diagnose", "task_type": "collections", "task_id": job}
    result = asyncio.run(finished(body))
    assert result["status"] == "completed" and len(result["evidence"]) == 3
    assert result["input_tokens"] == 8 and result["output_tokens"] == 9
    assert "MODEL-SECRET" not in json.dumps(result)
    assert fake.results[0][1]["id"] == "e3"
    # Same request replays a persisted record and never invokes the model again.
    assert asyncio.run(finished(body))["id"] == result["id"]
    assert fake.complete_count == 2
    with pytest.raises(HTTPException) as err:
        runtime.start_run(runtime.RunInput(**{**body, "prompt": "different"}))
    assert err.value.status_code == 409


def test_runtime_rejects_scope_escape_and_unknown_tools(local_project, monkeypatch):
    enabled(); account()
    fake = install_model(monkeypatch, [Turn(calls=[ToolCall("1", "list_accounts", {"platform": "xhs"}),
                                                  ToolCall("2", "delete_account", {"id": 1})]), Turn(text="证据不足")])
    result = asyncio.run(finished({"prompt": "diagnose", "platform": "douyin"}))
    assert result["status"] == "completed"
    assert all("error" in value for _, value in fake.results)


def test_run_api_creates_polls_and_replays_without_another_model_call(local_project, monkeypatch):
    enabled()
    acc = account()
    fake = install_model(monkeypatch, [Turn(text="依据 e2：当前没有失败任务")])
    async def scenario():
        async with client() as http:
            context = await http.get("/api/agent/context?platform=douyin")
            assert context.status_code == 200 and context.json()["metrics"]["accounts"] == 1
            body = {"request_id": "api-lifecycle-fixture-001", "prompt": "检查近期状态", "platform": "douyin", "account_id": acc}
            response = await http.post("/api/agent/runs", json=body)
            assert response.status_code == 202
            handle = runtime.RUNNING.get(body["request_id"])
            if handle:
                await handle
            result = await http.get("/api/agent/runs/" + body["request_id"])
            assert result.json()["status"] == "completed" and len(result.json()["evidence"]) == 3
            repeated = await http.post("/api/agent/runs", json=body)
            assert repeated.json() == result.json()
            records = await http.get("/api/agent/runs?platform=douyin")
            assert len(records.json()) == 1 and "evidence" not in records.json()[0]
            assert fake.complete_count == 1
    asyncio.run(scenario())


def test_running_limit_and_cancellation_retain_final_audit(local_project, monkeypatch):
    enabled()
    async def scenario():
        started = asyncio.Event()
        class WaitingModel:
            async def complete(self, _):
                started.set()
                await asyncio.Event().wait()
        monkeypatch.setattr(runtime, "ModelSession", lambda *_: WaitingModel())
        first = runtime.start_run(runtime.RunInput(prompt="first"))
        await asyncio.wait_for(started.wait(), timeout=5)
        second = runtime.start_run(runtime.RunInput(prompt="second"))
        with pytest.raises(HTTPException) as error:
            runtime.start_run(runtime.RunInput(prompt="third"))
        assert error.value.status_code == 429
        stopped = await runtime.cancel_run(first["id"])
        assert stopped["status"] == "canceled" and stopped["finished_at"]
        assert len(stopped["evidence"]) == 3
        assert stopped["steps"][-1]["status"] == "stopped"
        await runtime.cancel_run(second["id"])
        assert not runtime.RUNNING
    asyncio.run(scenario())


def test_runtime_bound_and_failure_keeps_evidence(local_project, monkeypatch):
    enabled(max_rounds=1)
    install_model(monkeypatch, [Turn(calls=[ToolCall("c1", "list_tasks", {})])])
    result = asyncio.run(finished({"prompt": "analyze"}))
    assert result["status"] == "failed" and "轮数" in result["error"]
    assert result["evidence"] and result["finished_at"]


def test_cancel_and_restart_do_not_auto_execute(local_project, monkeypatch):
    enabled()
    class SlowModel(FakeModel):
        async def complete(self, _):
            await asyncio.sleep(100)
    monkeypatch.setattr(runtime, "ModelSession", lambda *_: SlowModel([]))
    async def scenario():
        result = runtime.start_run(runtime.RunInput(prompt="wait"))
        await asyncio.sleep(0.05)
        assert (await runtime.cancel_run(result["id"]))["status"] == "canceled"
        assert not runtime.RUNNING
    asyncio.run(scenario())
    store(AgentRun(id="restart", request_hash="hash", status="running"))
    runtime.recover_interrupted()
    assert runtime.get_run("restart")["status"] == "interrupted"


def test_workflow_is_preview_only_until_exact_confirmation(local_project, monkeypatch):
    enabled()
    acc = account()
    install_model(monkeypatch, [Turn(calls=[ToolCall("plan", "propose_workflow", plan_args(acc))])])
    result = asyncio.run(finished({"prompt": "创建摄影关键词采集", "mode": "workflow", "platform": "douyin"}))
    assert result["status"] == "completed" and len(result["workflows"]) == 1
    workflow = result["workflows"][0]
    with db.get_session() as s:
        assert not s.exec(select(KeywordCollectionJob)).all()
    monkeypatch.setattr(main, "engine", None)
    async def scenario():
        async with client() as http:
            path = f"/api/agent/workflows/{workflow['id']}/apply"
            assert (await http.post(path, json={"confirmed": False, "plan_hash": workflow["plan_hash"]})).status_code == 422
            assert (await http.post(path, json={"confirmed": True, "plan_hash": "0" * 64})).status_code == 409
            approved = {"confirmed": True, "plan_hash": workflow["plan_hash"]}
            first = await http.post(path, json=approved)
            assert first.status_code == 200 and first.json()["status"] == "applied", first.text
            assert (await http.post(path, json=approved)).json()["results"] == first.json()["results"]
    asyncio.run(scenario())
    with db.get_session() as s:
        jobs = s.exec(select(KeywordCollectionJob)).all()
        assert len(jobs) == 1 and jobs[0].max_comments_per_content == 0
        assert not jobs[0].download_media and len(s.exec(select(TaskSubmission)).all()) == 1


def test_monitor_creation_receipt_is_atomic_and_replayable(local_project, monkeypatch):
    enabled()
    acc = account("xhs")
    args = plan_args(acc, kind="create_monitor", platform="xhs", keywords=[], monitor_kind="keyword", target="摄影")
    plan = save_plan("monitor-run", validate_plan(args))
    executors = {"create_monitor": main._agent_create_monitor}
    result = asyncio.run(apply_plan(plan["id"], plan["plan_hash"], executors))
    assert result["status"] == "applied", result
    # Simulate loss of workflow progress after the monitor transaction committed.
    with db.get_session() as s:
        row = s.get(AgentWorkflow, plan["id"]); row.status = "interrupted"; row.results_json = "[]"
        s.add(row); s.commit()
    result = asyncio.run(apply_plan(plan["id"], plan["plan_hash"], executors))
    assert result["status"] == "applied" and result["results"][0]["replayed"]
    with db.get_session() as s:
        assert len(s.exec(select(MonitorTarget)).all()) == 1


def test_partial_workflow_keeps_successful_steps_and_revalidates(local_project, monkeypatch):
    acc = account()
    args = plan_args(acc)
    args["steps"].append({**args["steps"][0], "keywords": ["另一项"]})
    plan = save_plan("partial-run", validate_plan(args))
    calls = []
    async def execute(payload, request):
        calls.append(payload["keywords"])
        if len(calls) == 2:
            raise HTTPException(400, "模拟业务校验失败")
        return {"id": len(calls), "status": "pending"}
    result = asyncio.run(apply_plan(plan["id"], plan["plan_hash"], {"create_collection": execute}))
    assert result["status"] == "partial" and len(result["results"]) == 1
    result = asyncio.run(apply_plan(plan["id"], plan["plan_hash"], {"create_collection": execute}))
    assert result["status"] == "applied" and len(calls) == 3


def test_workflow_parallel_confirmations_claim_one_execution(local_project):
    plan = save_plan("concurrent-plan", validate_plan(plan_args(account())))
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def execute(payload, request):
            calls.append(request.headers["idempotency-key"])
            entered.set()
            await release.wait()
            return {"id": 1, "status": "pending"}
        executors = {"create_collection": execute}
        first = asyncio.create_task(apply_plan(plan["id"], plan["plan_hash"], executors))
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            with pytest.raises(HTTPException) as error:
                await apply_plan(plan["id"], plan["plan_hash"], executors)
            assert error.value.status_code == 409
        finally:
            release.set()
        assert (await first)["status"] == "applied" and len(calls) == 1
    asyncio.run(scenario())


def test_plan_validation_rejects_shell_fields_and_scope(local_project):
    acc = account()
    for args in [plan_args(acc, command="rm -rf"), plan_args(acc, account_id=999),
                 plan_args(acc, kind="create_monitor", keywords=[], target="http://127.0.0.1:8000/api/accounts")]:
        with pytest.raises(ValueError):
            validate_plan(args)
    with pytest.raises(ValueError):
        validate_plan(plan_args(acc), platform="xhs")


def test_redaction_removes_nested_secrets_and_url_credentials():
    text = json.dumps(redact({"storage_state": "SECRET", "detail": "https://user:PASS@host/path?token=SECRET password=HIDDEN", "nested": {"api_key": "SECRET"}}))
    assert all(value not in text for value in ("SECRET", "PASS", "HIDDEN"))
