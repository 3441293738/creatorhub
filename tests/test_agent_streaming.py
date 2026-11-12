"""Stream-only acceptance: chunk boundaries, previews, usage, tools and stop."""
import asyncio
import json

import httpx
import pytest

from app.agent import providers, runtime
from app.agent.api import run_events
from app.agent.config import AgentSettings
from app.agent.providers import ModelError, ModelSession
from app.agent.streaming import NativeStream, SSEDecoder, StreamError
from app.models import AgentRun
from agent_stream_fixture import event, frames, response
from test_agent import client, enabled, finished, store
from test_project_optimizations import local_project


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, gate=None, gate_at=1):
        self.chunks, self.gate, self.closed = chunks, gate, False
        self.gate_at = gate_at
    async def __aiter__(self):
        for i, chunk in enumerate(self.chunks):
            if i == self.gate_at and self.gate: await self.gate.wait()
            yield chunk
    async def aclose(self): self.closed = True


def mock_stream(monkeypatch, stream):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})
    real = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return requests


def chat_delta(**delta):
    return event({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})


def chat_end(reason="stop", usage=None):
    return event({"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}) + event({"choices": [], "usage": usage}) + event("[DONE]")


@pytest.mark.parametrize("provider", ["openai_compatible", "openai_responses", "anthropic"])
def test_requests_are_always_streaming_including_connection_tests(provider):
    url, headers, body = ModelSession(AgentSettings(base_url="https://model.example/v1", provider=provider), "s", "p").request([])
    assert body["stream"] is True and headers["Accept"] == "text/event-stream"
    assert ("stream_options" in body) == (provider == "openai_compatible")
    if provider == "openai_compatible": assert body["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize("split", [1, 2, 7, 51, 4096])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_sse_decoder_handles_utf8_bom_crlf_multiline_data_and_comments(split, newline):
    raw = ('\ufeff: heartbeat\nevent: custom\ndata: {"text":\ndata: "中文"}\n\n').replace("\n", newline).encode()
    decoder, received = SSEDecoder(), []
    for start in range(0, len(raw), split): received.extend(decoder.feed(raw[start:start + split]))
    assert len(received) == 1 and received[0][0] == "custom"
    assert json.loads(received[0][1]) == {"text": "中文"}


def test_chat_preview_arrives_before_completion_and_secret_fragments_do_not_flash(monkeypatch):
    async def scenario():
        gate = asyncio.Event()
        raw1 = (chat_delta(content="中文前缀 SECR") + chat_delta(reasoning_content="公开摘要 SECR")).encode()
        raw2 = (chat_delta(content="ET-KEY 后文", reasoning_content="ET-KEY 后文") + chat_end(usage={"prompt_tokens": 12, "completion_tokens": 8})).encode()
        upstream = Chunks([raw1, raw2], gate=gate)
        calls = mock_stream(monkeypatch, upstream)
        model = ModelSession(AgentSettings(base_url="https://model.example/v1", api_key="SECRET-KEY"), "s", "p")
        progress = []
        model.on_progress = lambda text, usage: progress.append((text, usage))
        task = asyncio.create_task(model.complete([]))
        for _ in range(30):
            if progress: break
            await asyncio.sleep(.01)
        assert not task.done() and progress[0][0] == "中文前缀 "
        assert "SECR" not in json.dumps(progress) and len(model.history) == 2
        gate.set(); turn = await task
        assert turn.text == "中文前缀 SECRET-KEY 后文"  # Final redaction is at runtime persistence.
        assert "SECRET-KEY" not in json.dumps(progress)
        assert turn.input_tokens == 12 and turn.output_tokens == 8
        assert len(calls) == 1 and upstream.closed
    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["openai_compatible", "openai_responses", "anthropic"])
def test_tool_calls_require_terminal_event_and_valid_complete_arguments(provider, monkeypatch):
    if provider == "openai_compatible":
        payload = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c", "function": {"name": "list_accounts", "arguments": "{}"}}]}}]}
    elif provider == "anthropic":
        payload = {"content": [{"type": "tool_use", "id": "c", "name": "list_accounts", "input": {}}]}
    else:
        payload = {"output": [{"type": "function_call", "call_id": "c", "name": "list_accounts", "arguments": "{}"}]}
    raw = frames(provider, payload)
    terminal = {"openai_compatible": "data: [DONE]", "anthropic": 'data: {"type": "message_stop"}', "openai_responses": 'data: {"type": "response.completed"'}[provider]
    # Responses terminal carries the complete payload; a done item alone is not enough.
    incomplete = raw[:raw.rfind(terminal)] if provider != "openai_responses" else event({"type": "response.output_item.done", "output_index": 0, "item": payload["output"][0]})
    requests = mock_stream(monkeypatch, Chunks([incomplete.encode()]))
    model = ModelSession(AgentSettings(base_url="https://model.example/v1", provider=provider), "s", "p")
    before = list(model.history)
    with pytest.raises(ModelError, match="中断"): asyncio.run(model.complete([]))
    assert model.history == before and len(requests) == 1


def test_chat_interleaved_tools_accumulate_arguments_and_usage_only_once(monkeypatch):
    raw = chat_delta(tool_calls=[{"index": 0, "id": "a", "function": {"name": "get_metrics", "arguments": '{"days":'}},
                                 {"index": 1, "id": "b", "function": {"name": "list_accounts", "arguments": "{"}}])
    raw += chat_delta(tool_calls=[{"index": 1, "function": {"arguments": "}"}}, {"index": 0, "function": {"arguments": "1}"}}])
    raw += chat_end("tool_calls", {"prompt_tokens": 10, "completion_tokens": 20})
    mock_stream(monkeypatch, Chunks([raw[i:i + 9].encode() for i in range(0, len(raw), 9)]))
    turn = asyncio.run(ModelSession(AgentSettings(base_url="https://model.example/v1", ), "s", "p").complete([]))
    assert [c.arguments for c in turn.calls] == [{"days": 1}, {}]
    assert turn.telemetry["usage"]["total_tokens"] == 30


def test_anthropic_deltas_preserve_signature_and_normalize_cumulative_usage(monkeypatch):
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 30, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "公开说明"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "OPAQUE-SIGNATURE"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "c", "name": "get_metrics", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"days":'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '2}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 15}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ]
    mock_stream(monkeypatch, Chunks([event(e, e["type"]).encode() for e in events]))
    model = ModelSession(AgentSettings(base_url="https://model.example/v1", provider="anthropic"), "s", "p")
    previews = []; model.on_progress = lambda text, telemetry: previews.append(telemetry)
    turn = asyncio.run(model.complete([]))
    assert turn.calls[0].arguments == {"days": 2}
    assert (turn.input_tokens, turn.output_tokens) == (40, 20)
    assert model.history[-1]["content"][1]["input"] == {"days": 2}
    assert model.history[-1]["content"][0]["signature"] == "OPAQUE-SIGNATURE"
    assert "OPAQUE" not in json.dumps(previews) and len(previews) > 3
    assert not previews[-2]["usage_final"] and previews[-1]["usage_final"]


def test_responses_text_summary_deltas_and_done_snapshot_are_not_appended_twice(monkeypatch):
    output = [{"type": "reasoning", "id": "r", "summary": [{"type": "summary_text", "text": "摘要说明"}], "encrypted_content": "OPAQUE-REASONING"},
              {"type": "message", "content": [{"type": "output_text", "text": "最终答案"}]}]
    events = [
        {"type": "response.created", "response": {"id": "resp1"}},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "delta": "摘要"},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "delta": "说明"},
        {"type": "response.reasoning_summary_text.done", "output_index": 0, "summary_index": 0, "text": "摘要说明"},
        {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": "最终"},
        {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": "答案"},
        {"type": "response.completed", "response": {"id": "resp1", "output": output, "usage": {"input_tokens": 12, "output_tokens": 15}}},
    ]
    mock_stream(monkeypatch, Chunks([event(e).encode() for e in events]))
    model = ModelSession(AgentSettings(base_url="https://model.example/v1", provider="openai_responses"), "s", "p")
    previews = []; model.on_progress = lambda text, telemetry: previews.append((text, telemetry))
    turn = asyncio.run(model.complete([]))
    assert turn.text == "最终答案" and turn.telemetry["reasoning"]["text"] == "摘要说明"
    assert any(t == "最终" for t, _ in previews) and "OPAQUE" not in json.dumps(previews)
    assert "OPAQUE-REASONING" in json.dumps(model.history)


@pytest.mark.parametrize("raw", [
    chat_delta(tool_calls=[{"index": 0, "id": "c", "function": {"name": "read", "arguments": '{"secret":'}}]) + chat_end("tool_calls"),
    chat_delta(content="partial") + event({"error": {"message": "SECRET-RAW"}}),
    chat_delta(content="partial") + "data: invalid SECRET-RAW\n\n",
    chat_delta(content="partial") + chat_end("length", {"prompt_tokens": 10, "completion_tokens": 30}),
], ids=["incomplete-arguments", "error-event", "bad-json", "truncated"])
def test_stream_errors_never_commit_provider_history_or_retry(monkeypatch, raw):
    calls = mock_stream(monkeypatch, Chunks([raw.encode()]))
    model = ModelSession(AgentSettings(base_url="https://model.example/v1", ), "s", "p")
    before = list(model.history)
    with pytest.raises(ModelError) as error: asyncio.run(model.complete([]))
    assert "SECRET-RAW" not in str(error.value) and model.history == before and len(calls) == 1


def test_non_stream_json_is_rejected_without_fallback_or_paid_retry(monkeypatch):
    requests = []
    real = httpx.AsyncClient
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Do not accept this"}}]})
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ModelError, match="仅支持流式"):
        asyncio.run(ModelSession(AgentSettings(base_url="https://model.example/v1", ), "s", "p").complete([]))
    assert len(requests) == 1 and json.loads(requests[0].content)["stream"] is True


def test_stream_bounds_and_bad_indices_are_rejected():
    with pytest.raises(StreamError): SSEDecoder().feed(b"data: " + b"x" * 1_000_001)
    stream = NativeStream("openai_compatible")
    with pytest.raises(StreamError): stream.feed("message", json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 99999999}]}}]}))


@pytest.mark.parametrize("provider", ["anthropic", "openai_responses"])
def test_preview_bounds_include_all_text_blocks(provider):
    stream = NativeStream(provider)
    if provider == "anthropic":
        stream.blocks = {n: {"type": "text", "text": "x" * 20000} for n in range(2)}
    else:
        stream.items = {n: {"type": "message", "content": [{"type": "output_text", "text": "x" * 20000}]} for n in range(2)}
    with pytest.raises(StreamError, match="正文"): stream.preview()


def test_anthropic_tool_stop_reason_must_match_assembled_blocks(monkeypatch):
    payload = {"content": [{"type": "tool_use", "id": "c", "name": "list_accounts", "input": {}}], "stop_reason": "end_turn"}
    mock_stream(monkeypatch, Chunks([frames("anthropic", payload).encode()]))
    session = ModelSession(AgentSettings(base_url="https://model.example/v1", provider="anthropic"), "s", "p")
    with pytest.raises(ModelError, match="结束状态不一致"): asyncio.run(session.complete([]))
    assert len(session.history) == 1


def test_anthropic_mismatched_delta_is_not_silently_ignored():
    stream = NativeStream("anthropic")
    stream.feed("message_start", '{"type":"message_start","message":{}}')
    stream.feed("content_block_start", '{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","input":{}}}')
    with pytest.raises(StreamError, match="不匹配"):
        stream.feed("content_block_delta", '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"{}"}}')


def test_timeout_preserves_usage_closes_upstream_and_never_retries(monkeypatch):
    async def scenario():
        partial = chat_delta(content="keep") + event({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
        chunks = Chunks([partial.encode(), chat_end().encode()], gate=asyncio.Event())
        requests = mock_stream(monkeypatch, chunks)
        cfg = AgentSettings(base_url="https://model.example/v1").model_copy(update={"timeout_seconds": .05})
        model = ModelSession(cfg, "s", "p")
        with pytest.raises(ModelError, match="超时") as error: await model.complete([])
        assert error.value.telemetry["usage"]["total_tokens"] == 12
        assert not error.value.telemetry["usage_final"] and chunks.closed and len(requests) == 1
        assert len(model.history) == 2
    asyncio.run(scenario())


def test_runtime_streams_partial_text_then_finishes_and_counts_latest_usage_once(local_project, monkeypatch):
    enabled()
    async def scenario():
        gate = asyncio.Event()
        chunks = Chunks([chat_delta(content="先收到一部分").encode(), (chat_delta(content="，然后完成") + chat_end(usage={"prompt_tokens": 20, "completion_tokens": 10})).encode()], gate=gate)
        requests = mock_stream(monkeypatch, chunks)
        row = runtime.start_run(runtime.RunInput(prompt="stream"))
        for _ in range(80):
            partial = runtime.get_run(row["id"])
            if partial["partial_answer"]: break
            await asyncio.sleep(.01)
        assert partial["status"] == "running" and partial["partial_answer"] == "先收到一部分" and not partial["answer"]
        gate.set(); await runtime.RUNNING[row["id"]]
        result = runtime.get_run(row["id"])
        assert result["answer"] == "先收到一部分，然后完成" and result["partial_answer"] == ""
        assert result["usage"]["total_tokens"] == 30 and result["usage"]["requests"] == 1
        assert len(requests) == 1
    asyncio.run(scenario())


def test_stop_closes_upstream_stream_and_retains_partial_output(local_project, monkeypatch):
    enabled()
    async def scenario():
        chunks = Chunks([chat_delta(content="保留已接收内容").encode(), chat_end().encode()], gate=asyncio.Event())
        requests = mock_stream(monkeypatch, chunks)
        row = runtime.start_run(runtime.RunInput(prompt="stop"))
        for _ in range(80):
            if runtime.get_run(row["id"])["partial_answer"]: break
            await asyncio.sleep(.01)
        result = await runtime.cancel_run(row["id"])
        assert result["status"] == "canceled" and result["partial_answer"] == "保留已接收内容"
        assert chunks.closed and len(requests) == 1 and not result["workflows"]
    asyncio.run(scenario())


def test_last_burst_delta_is_flushed_even_when_upstream_pauses(local_project, monkeypatch):
    enabled()
    async def scenario():
        chunks = Chunks([chat_delta(content="第一段").encode(), chat_delta(content="，同批最后一段").encode(),
                         chat_end().encode()], gate=asyncio.Event(), gate_at=2)
        mock_stream(monkeypatch, chunks)
        row = runtime.start_run(runtime.RunInput(prompt="burst"))
        try:
            for _ in range(100):
                result = runtime.get_run(row["id"])
                if result["partial_answer"] == "第一段，同批最后一段": break
                await asyncio.sleep(.01)
            assert result["status"] == "running" and result["partial_answer"] == "第一段，同批最后一段"
        finally:
            await runtime.cancel_run(row["id"])
        assert chunks.closed
    asyncio.run(scenario())


def test_runtime_never_saves_workflow_from_unterminated_tool_stream(local_project, monkeypatch):
    enabled()
    raw = chat_delta(content="正在生成草稿", tool_calls=[{"index": 0, "id": "c", "function": {
        "name": "propose_workflow", "arguments": json.dumps({"title": "incomplete", "steps": []})}}])
    raw += event({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    calls = mock_stream(monkeypatch, Chunks([raw.encode()]))
    invoked = []
    monkeypatch.setattr(runtime, "save_plan", lambda *_: invoked.append(True))
    result = asyncio.run(finished({"prompt": "create", "mode": "workflow"}))
    assert result["status"] == "failed" and "中断" in result["error"]
    assert result["partial_answer"] == "正在生成草稿" and not result["workflows"]
    assert not invoked and len(calls) == 1


def test_browser_sse_endpoint_reconnect_is_read_only_and_same_origin(local_project):
    store(AgentRun(id="sse-record-00000001", request_hash="x", status="completed", answer="已保存"))
    async def scenario():
        async with client() as http:
            for _ in range(2):
                result = await http.get("/api/agent/runs/sse-record-00000001/events")
                assert result.status_code == 200 and result.headers["content-type"].startswith("text/event-stream")
                assert "event: snapshot" in result.text and "已保存" in result.text and "event: done" in result.text
                assert result.headers["x-accel-buffering"] == "no"
            assert (await http.get("/api/agent/runs/missing/events")).status_code == 404
            assert (await http.get("/api/agent/runs/sse-record-00000001/events", headers={"Origin": "https://evil.example"})).status_code == 403
    asyncio.run(scenario())


def test_sse_generator_yields_running_snapshot_before_model_finishes(local_project):
    run_id = "sse-live-record-00001"
    store(AgentRun(id=run_id, request_hash="x", status="running", partial_answer="live"))
    class Request:
        async def is_disconnected(self): return False
    async def scenario():
        generator = run_events(run_id, Request())
        first = await generator.__anext__()
        assert '"partial_answer":"live"' in first and '"status":"running"' in first
        runtime.update_run(run_id, status="completed", answer="complete", partial_answer="")
        final = await generator.__anext__()
        assert '"answer":"complete"' in final
        assert "event: done" in await generator.__anext__()
        await generator.aclose()
    asyncio.run(scenario())
