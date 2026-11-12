"""Credential-free diagnostics for routes returning HTML or incompatible JSON."""
import asyncio
import json

import httpx
import pytest

from app.agent import providers
from app.agent.config import AgentSettings
from app.agent.providers import ModelError, ModelSession
from agent_stream_fixture import response as stream_response


@pytest.mark.parametrize("protocol,suffix", [("anthropic", "/messages"), ("openai_responses", "/responses"), ("openai_compatible", "/chat/completions")])
@pytest.mark.parametrize("base,mode,prefix", [("https://model.example", "auto", "/v1"), ("https://model.example/v1", "auto", "/v1"), ("https://model.example/custom/v2", "auto", "/custom/v2"), ("https://model.example", "exact", "")])
def test_inference_path_is_deterministic_and_never_double_prefixes(protocol, suffix, base, mode, prefix):
    cfg = AgentSettings(base_url=base, provider=protocol, api_path_mode=mode)
    url, _, body = ModelSession(cfg, "s", "p").request([])
    assert url == "https://model.example" + prefix + suffix
    assert body["stream"] is True


@pytest.mark.parametrize("content,content_type,expected", [
    (b"<!doctype html><html>PRIVATE gateway page</html>", "text/html", "HTML"),
    (b"<html>PRIVATE page</html>", "application/json", "SSE"),
    (b"event: message_start\ndata: PRIVATE\n\n", "text/event-stream", "JSON"),
    (b"", "text/event-stream", "中断"),
    (b"PRIVATE not-json", "application/json", "SSE"),
], ids=["html", "mislabeled-html", "invalid-event", "empty", "non-stream"])
def test_specific_http_response_diagnostics_without_echo_or_retry(monkeypatch, content, content_type, expected):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=content, headers={"Content-Type": content_type})
    real = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    cfg = AgentSettings(base_url="https://model.example", provider="anthropic", api_key="PRIVATE")
    model = ModelSession(cfg, "s", "p")
    with pytest.raises(ModelError, match=expected) as error:
        asyncio.run(model.complete([]))
    assert "PRIVATE" not in str(error.value) and len(requests) == 1
    assert requests[0].url.path == "/v1/messages" and len(model.history) == 1


@pytest.mark.parametrize("provider,payload,expected", [
    ("anthropic", {"choices": []}, "OpenAI Chat Completions"),
    ("openai_responses", {"content": []}, "Anthropic Messages"),
    ("openai_compatible", {"output": []}, "OpenAI Responses"),
    ("anthropic", {"data": []}, "content 数组"),
    ("anthropic", {"error": {"message": "PRIVATE"}}, "错误对象"),
    ("anthropic", [], "JSON 对象"),
    ("openai_compatible", {"choices": []}, "字段"),
    ("anthropic", {"content": []}, "未返回"),
], ids=["chat-mismatch", "anthropic-mismatch", "responses-mismatch", "catalog", "upstream-error", "array", "empty-choices", "empty-content"])
def test_native_protocol_shape_and_empty_diagnostics(provider, payload, expected):
    session = ModelSession(AgentSettings(provider=provider), "s", "p")
    before = list(session.history)
    with pytest.raises(ModelError, match=expected) as error:
        session.parse(payload)
    assert "PRIVATE" not in str(error.value) and session.history == before


def test_bad_tool_arguments_do_not_pollute_next_turn_history():
    model = ModelSession(AgentSettings(provider="openai_responses"), "s", "p")
    payload = {"output": [{"type": "function_call", "call_id": "c", "name": "read", "arguments": '{"PRIVATE":'}]}
    with pytest.raises(ModelError, match="工具参数") as error:
        model.parse(payload)
    assert "PRIVATE" not in str(error.value) and len(model.history) == 1


def test_chat_content_blocks_are_supported():
    model = ModelSession(AgentSettings(), "s", "p")
    turn = model.parse({"choices": [{"message": {"content": [{"type": "text", "text": "OK"}]}}]})
    assert turn.text == "OK"


def test_anthropic_root_regression_is_fixed_without_paid_retries(monkeypatch):
    paths = []
    def upstream(request):
        paths.append(request.url.path)
        if request.url.path == "/v1/messages":
            return stream_response("anthropic", {"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn"})
        return httpx.Response(200, text="<!doctype html><html>Gateway UI</html>", headers={"Content-Type": "text/html"})
    real = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(upstream), **kw))
    model = ModelSession(AgentSettings(base_url="https://model.example", provider="anthropic"), "s", "p")
    assert asyncio.run(model.complete([])).text == "OK"
    assert paths == ["/v1/messages"]
