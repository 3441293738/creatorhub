"""Offline provider telemetry contracts: no real model calls or billing."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from app.agent import runtime
from app.agent.config import AgentSettings
from app.agent.providers import ModelError, ModelSession, ToolCall, Turn
from app.agent.telemetry import REASONING_LIMIT, RUN_REASONING_LIMIT, extract_telemetry, summarize_usage
from app.models import AgentRun
from test_agent import client, enabled, finished, install_model, store
from test_project_optimizations import local_project


def chat(usage=None, **message):
    return {"choices": [{"message": {"content": "Final answer", **message}}], "usage": usage}


@pytest.mark.parametrize("provider,payload,expected", [
    ("openai_compatible", chat({"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180,
        "prompt_tokens_details": {"cached_tokens": 60}, "completion_tokens_details": {"reasoning_tokens": 50}},
        reasoning_content="Public rationale SECRET-KEY"), (100, 80, 180, 60, 50)),
    ("openai_responses", {"output": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "Public summary SECRET-KEY"}],
        "encrypted_content": "NEVER-EXPOSE-ENCRYPTED"}, {"type": "message", "content": [{"type": "output_text", "text": "Final answer"}]}],
        "usage": {"input_tokens": 100, "output_tokens": 80, "input_tokens_details": {"cached_tokens": 60},
                  "output_tokens_details": {"reasoning_tokens": 50}}}, (100, 80, 180, 60, 50)),
    ("anthropic", {"content": [{"type": "thinking", "thinking": "Public thinking SECRET-KEY", "signature": "NEVER-EXPOSE-SIGNATURE"},
        {"type": "redacted_thinking", "data": "NEVER-EXPOSE-REDACTED"}, {"type": "text", "text": "Final answer"}],
        "usage": {"input_tokens": 20, "cache_read_input_tokens": 60, "cache_creation_input_tokens": 20, "output_tokens": 80,
                  "output_tokens_details": {"thinking_tokens": 50}}}, (100, 80, 180, 60, 50)),
], ids=["compatible", "responses", "anthropic"])
def test_public_reasoning_and_tokens_are_normalized_without_double_counting(provider, payload, expected):
    model = ModelSession(AgentSettings(provider=provider, api_key="SECRET-KEY"), "s", "p")
    turn = model.parse(payload)
    usage = turn.telemetry["usage"]
    assert tuple(usage[key] for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")) == expected
    assert (turn.input_tokens, turn.output_tokens) == (100, 80)
    assert turn.text == "Final answer" and "Public" in turn.telemetry["reasoning"]["text"]
    serialized = json.dumps(turn.telemetry)
    assert "SECRET-KEY" not in serialized and "NEVER-EXPOSE" not in serialized
    # Opaque fields stay intact only in native in-memory tool-loop history.
    if provider != "openai_compatible":
        assert "NEVER-EXPOSE" in json.dumps(model.history)


@pytest.mark.parametrize("usage", [None, {}, [], {"prompt_tokens": -1, "completion_tokens": True},
    {"prompt_tokens": "NaN", "completion_tokens": 1.5}, {"input_tokens": 10**20, "output_tokens": "SECRET"}])
def test_missing_or_malformed_usage_does_not_break_text_and_is_not_zero(usage):
    turn = ModelSession(AgentSettings(), "s", "p").parse(chat(usage))
    assert turn.text == "Final answer"
    assert turn.telemetry["usage"]["input_tokens"] is None
    assert turn.telemetry["usage"]["output_tokens"] is None
    summary = summarize_usage([turn.telemetry])
    assert summary["status"] == "unavailable" and summary["total_tokens"] is None


def test_explicit_zero_numeric_string_and_cached_deepseek_fields():
    turn = ModelSession(AgentSettings(), "s", "p").parse(chat({"prompt_tokens": "100", "completion_tokens": 0,
        "prompt_cache_hit_tokens": 80, "completion_tokens_details": {"reasoning_tokens": 0}}))
    summary = summarize_usage([turn.telemetry])
    assert summary["status"] == "reported" and summary["total_tokens"] == 100
    assert summary["output_tokens"] == 0 and summary["reasoning_tokens"] == 0 and summary["cached_input_tokens"] == 80


def test_invalid_cache_and_reasoning_details_are_not_added_to_totals():
    telemetry = extract_telemetry(chat({"prompt_tokens": 10, "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 90}, "completion_tokens_details": {"reasoning_tokens": 90}}), "openai_compatible")
    assert telemetry["usage"]["total_tokens"] == 15
    assert telemetry["usage"]["cached_input_tokens"] is None and telemetry["usage"]["reasoning_tokens"] is None
    broken = extract_telemetry({"usage": {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": "bad"}}, "anthropic")
    assert broken["usage"]["input_tokens"] is None and broken["usage"]["total_tokens"] is None


@pytest.mark.parametrize("cost,currency,expected", [(0.12345, "USD", "0.12345"), (0, "CNY", "0"),
    (1, None, None), (-1, "USD", None), ("NaN", "USD", None), ("Infinity", "CNY", None), (False, "USD", None),
    ({"amount": "0.012300", "currency": "USD"}, None, "0.012300")])
def test_cost_requires_valid_explicit_amount_and_currency(cost, currency, expected):
    result = extract_telemetry(chat({"cost": cost, "cost_currency": currency}), "openai_compatible")["cost"]
    assert (result["amount"] if result else None) == expected


def test_partial_usage_and_mixed_currencies_remain_explicit():
    calls = [extract_telemetry(chat({"prompt_tokens": 10, "completion_tokens": 5, "cost": "0.01", "cost_currency": "USD"}), "openai_compatible"),
             extract_telemetry(chat({"prompt_tokens": 20, "completion_tokens": 5, "cost": "0.02", "cost_currency": "CNY"}), "openai_compatible"),
             {"usage": {}}]
    usage = summarize_usage(calls)
    assert usage["status"] == "partial" and usage["total_tokens"] == 40 and usage["reported_requests"] == 2
    assert usage["costs"] == [{"currency": "USD", "amount": "0.01", "source": "upstream"}, {"currency": "CNY", "amount": "0.02", "source": "upstream"}]
    assert not usage["cost_complete"]


def test_reasoning_is_bounded_after_redaction_and_opaque_payloads_are_not_used_as_text():
    result = extract_telemetry(chat(reasoning_content="a" * (REASONING_LIMIT - 5) + "SECRET-KEY" + "b" * 30), "openai_compatible", secrets=("SECRET-KEY",))
    assert len(result["reasoning"]["text"]) <= REASONING_LIMIT and result["reasoning"]["truncated"]
    assert "SECRET" not in result["reasoning"]["text"]
    for provider, body in [("anthropic", {"content": [{"type": "redacted_thinking", "data": "PRIVATE"}]}),
                           ("openai_responses", {"output": [{"type": "reasoning", "summary": [], "encrypted_content": "PRIVATE"}]})]:
        reasoning = extract_telemetry(body, provider)["reasoning"]
        assert reasoning["protected"] and reasoning["text"] == "" and "PRIVATE" not in json.dumps(reasoning)


@pytest.mark.parametrize("provider,payload", [
    ("openai_compatible", {"choices": [{"finish_reason": "length", "message": {"reasoning_content": "Short public rationale"}}]}),
    ("openai_responses", {"status": "incomplete", "output": []}),
    ("anthropic", {"stop_reason": "max_tokens", "content": []}),
])
def test_failed_generation_preserves_reported_usage_without_accepting_partial_tool_calls(provider, payload):
    payload["usage"] = {"input_tokens": 100, "output_tokens": 50}
    model = ModelSession(AgentSettings(provider=provider), "s", "p")
    before = list(model.history)
    with pytest.raises(ModelError) as error:
        model.parse(payload)
    assert error.value.telemetry["usage"]["total_tokens"] == 150 and model.history == before


@pytest.mark.parametrize("provider,mode,field,expected", [
    ("openai_responses", "summary", "reasoning", {"summary": "auto"}),
    ("anthropic", "adaptive", "thinking", {"type": "adaptive"}),
    ("anthropic", "enabled", "thinking", {"type": "enabled", "budget_tokens": 1024}),
    ("openai_compatible", "enabled", "thinking", {"type": "enabled"}),
])
def test_reasoning_requests_are_opt_in_and_protocol_specific(provider, mode, field, expected):
    _, _, plain = ModelSession(AgentSettings(provider=provider), "s", "p").request([])
    assert "reasoning" not in plain and "thinking" not in plain
    _, _, enabled_body = ModelSession(AgentSettings(provider=provider, reasoning_mode=mode), "s", "p").request([])
    assert enabled_body[field] == expected and enabled_body["stream"] is True


def test_reasoning_configuration_rejects_wrong_protocol_and_insufficient_budget():
    with pytest.raises(ValidationError):
        AgentSettings(provider="openai_compatible", reasoning_mode="summary")
    with pytest.raises(ValidationError):
        AgentSettings(provider="anthropic", reasoning_mode="enabled", max_output_tokens=1024)


def test_runtime_persists_each_model_call_usage_reasoning_and_no_opaque_material(local_project, monkeypatch):
    enabled(api_key="SECRET-KEY")
    model = ModelSession(AgentSettings(api_key="SECRET-KEY"), "s", "p")
    first = model.parse(chat({"prompt_tokens": 100, "completion_tokens": 20, "cost": "0.0001", "cost_currency": "USD"}, reasoning_content="Public check SECRET-KEY"))
    first.calls = [ToolCall("c1", "get_metrics", {"days": 1})]
    second = model.parse(chat({"prompt_tokens": 150, "completion_tokens": 30, "cost": "0.0002", "cost_currency": "USD"}, reasoning_content="Public conclusion"))
    fake = install_model(monkeypatch, [first, second])
    body = {"request_id": "telemetry-request-0001", "prompt": "Check the task"}
    async def scenario():
        result = await finished(body)
        assert result["status"] == "completed" and result["usage"]["total_tokens"] == 300
        assert result["usage"]["input_tokens"] == 250 and result["usage"]["output_tokens"] == 50
        assert result["usage"]["requests"] == 2 and result["usage"]["costs"][0]["amount"] == "0.0003"
        assert len(result["reasoning"]) == 2 and len(result["model_calls"]) == 2
        assert "SECRET-KEY" not in json.dumps(result)
        assert (await finished(body))["usage"] == result["usage"] and fake.complete_count == 2
        async with client() as http:
            restored = (await http.get(f"/api/agent/runs/{result['id']}/conversation")).json()["runs"][0]
            assert restored["usage"] == result["usage"] and restored["reasoning"] == result["reasoning"]
            history = (await http.get("/api/agent/runs")).json()[0]
            assert "reasoning" not in history and history["usage"]["total_tokens"] == 300
    asyncio.run(scenario())


def test_runtime_retains_usage_on_failed_or_interrupted_next_call(local_project, monkeypatch):
    enabled()
    telemetry = extract_telemetry(chat({"prompt_tokens": 10, "completion_tokens": 5}, reasoning_content="Public failed summary"), "openai_compatible")
    class BrokenModel:
        async def complete(self, tools):
            raise ModelError("Fixture failure", telemetry=telemetry)
    monkeypatch.setattr(runtime, "ModelSession", lambda *_: BrokenModel())
    result = asyncio.run(finished({"prompt": "fail"}))
    assert result["status"] == "failed" and result["usage"]["total_tokens"] == 15
    assert result["model_calls"][0]["status"] == "failed" and result["reasoning"][0]["text"] == "Public failed summary"
    class SlowModel:
        def __init__(self): self.calls = 0
        def add_results(self, results): pass
        async def complete(self, tools):
            self.calls += 1
            if self.calls == 1:
                return Turn(calls=[ToolCall("one", "get_metrics", {})], telemetry=telemetry)
            await asyncio.sleep(30)
    monkeypatch.setattr(runtime, "ModelSession", lambda *_: SlowModel())
    async def scenario():
        row = runtime.start_run(runtime.RunInput(prompt="stop"))
        for _ in range(200):
            if len(runtime.get_run(row["id"])["model_calls"]) == 2:
                break
            await asyncio.sleep(.01)
        canceled = await runtime.cancel_run(row["id"])
        assert canceled["status"] == "canceled" and canceled["usage"]["total_tokens"] == 15
        assert canceled["usage"]["status"] == "partial" and canceled["usage"]["requests"] == 2
    asyncio.run(scenario())


def test_legacy_usage_is_labeled_without_inventing_zero_or_reasoning(local_project):
    for tokens in [0, 12]:
        run_id = f"legacy-usage-{tokens:06d}"
        store(AgentRun(id=run_id, request_hash="fixture", status="completed", input_tokens=tokens))
        run = runtime.get_run(run_id)
        assert run["usage"]["status"] == ("legacy" if tokens else "unavailable")
        assert run["usage"]["total_tokens"] == (tokens or None)
        assert run["reasoning"] == [] and run["usage"]["costs"] == []


def test_total_reasoning_is_bounded_across_tool_rounds(local_project, monkeypatch):
    enabled()
    model = ModelSession(AgentSettings(), "s", "p")
    turns = [model.parse(chat({"prompt_tokens": 10, "completion_tokens": 5}, reasoning_content="a" * REASONING_LIMIT)) for _ in range(4)]
    for i, turn in enumerate(turns[:-1]):
        turn.calls = [ToolCall(str(i), "get_metrics", {})]
    install_model(monkeypatch, turns)
    result = asyncio.run(finished({"prompt": "four rounds"}))
    assert result["status"] == "completed" and result["usage"]["total_tokens"] == 60
    assert sum(len(item["text"]) for item in result["reasoning"]) <= RUN_REASONING_LIMIT
    assert result["reasoning"][-1]["truncated"]


def test_service_restart_keeps_reported_usage_but_pending_call_is_interrupted(local_project):
    store(AgentRun(id="interrupted-usage-0001", request_hash="fixture", status="running", usage_json=json.dumps([
        {"round": 1, "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
        {"round": 2, "status": "running", "usage": {}},
    ])))
    runtime.recover_interrupted()
    result = runtime.get_run("interrupted-usage-0001")
    assert result["status"] == "interrupted" and result["model_calls"][-1]["status"] == "interrupted"
    assert result["usage"]["status"] == "partial" and result["usage"]["total_tokens"] == 15
