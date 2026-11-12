"""Synthetic upstream SSE fixtures; no network service or model key needed."""
import json

import httpx


def event(data, kind=None):
    return (("event: " + kind + "\n") if kind else "") + "data: " + (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)) + "\n\n"


def frames(provider, payload):
    if provider == "openai_responses":
        return event({"type": "response.completed", "response": {"status": "completed", **payload}})
    if provider == "anthropic":
        content = payload.get("content", [])
        output = event({"type": "message_start", "message": {"content": [], "usage": payload.get("usage", {})}})
        for index, block in enumerate(content):
            output += event({"type": "content_block_start", "index": index, "content_block": block})
            output += event({"type": "content_block_stop", "index": index})
        output += event({"type": "message_delta", "delta": {"stop_reason": payload.get("stop_reason") or ("tool_use" if any(b.get("type") == "tool_use" for b in content) else "end_turn")}, "usage": payload.get("usage", {})})
        return output + event({"type": "message_stop"})
    message = payload["choices"][0]["message"].copy()
    if message.get("tool_calls"):
        message["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
    reason = payload["choices"][0].get("finish_reason") or ("tool_calls" if message.get("tool_calls") else "stop")
    return (event({"choices": [{"index": 0, "delta": message, "finish_reason": None}]})
            + event({"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})
            + event({"choices": [], "usage": payload.get("usage")}) + event("[DONE]"))


def response(provider, payload):
    return httpx.Response(200, content=frames(provider, payload).encode(), headers={"Content-Type": "text/event-stream"})
