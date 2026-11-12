"""Small HTTP adapters; native tool results stay in their provider's format.

No SDK dependency, automatic retries, or automatic provider fallback. Custom
endpoints (including local OpenAI-compatible servers) may omit an API key.
"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field

import httpx

from .config import AgentSettings, inference_url
from .privacy import redact_stream
from .streaming import NativeStream, SSEDecoder, StreamError
from .telemetry import extract_telemetry


class ModelError(Exception):
    """Public, deliberately credential-free model failure."""

    def __init__(self, message, *, telemetry=None):
        super().__init__(message)
        self.telemetry = telemetry


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Turn:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    telemetry: dict = field(default_factory=dict)


def _arguments(value) -> dict:
    if isinstance(value, str):
        if len(value) > 16000:
            raise ModelError("模型工具参数过长")
        try:
            value = json.loads(value)
        except (ValueError, RecursionError) as exc:
            raise ModelError("模型工具参数不是完整的 JSON；请检查渠道的工具调用兼容性") from exc
    if not isinstance(value, dict):
        raise ModelError("模型工具参数须为 JSON 对象")
    return value


class ModelSession:
    def __init__(self, settings: AgentSettings, system: str, prompt: str):
        self.settings, self.system = settings, system
        self.on_progress = None
        self._last_telemetry = None
        self.history = [{"role": "user", "content": prompt}]
        if settings.provider == "openai_compatible":
            self.history.insert(0, {"role": "system", "content": system})

    def request(self, tools: list[dict]) -> tuple[str, dict, dict]:
        cfg = self.settings
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if cfg.api_key:
            headers["x-api-key" if cfg.provider == "anthropic" else "Authorization"] = (
                cfg.api_key if cfg.provider == "anthropic" else "Bearer " + cfg.api_key)
        definitions = [{"name": t["name"], "description": t["description"],
                        "parameters": t["inputSchema"]} for t in tools]
        body = {"model": cfg.model, "stream": True}
        if cfg.provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            body.update(system=self.system, messages=self.history, max_tokens=cfg.max_output_tokens)
            if cfg.reasoning_mode == "adaptive":
                body["thinking"] = {"type": "adaptive"}
            elif cfg.reasoning_mode == "enabled":
                body["thinking"] = {"type": "enabled", "budget_tokens": cfg.thinking_budget}
            if tools:
                body["tools"] = [{"name": t["name"], "description": t["description"],
                                  "input_schema": t["inputSchema"]} for t in tools]
        elif cfg.provider == "openai_responses":
            body.update(instructions=self.system, input=self.history,
                        max_output_tokens=cfg.max_output_tokens, store=False,
                        include=["reasoning.encrypted_content"])
            if cfg.reasoning_mode == "summary":
                body["reasoning"] = {"summary": "auto"}
            if tools:
                body["tools"] = [{"type": "function", "strict": False, **d} for d in definitions]
        else:
            body.update(messages=self.history)
            body["stream_options"] = {"include_usage": True}
            body[cfg.output_token_parameter] = cfg.max_output_tokens
            if cfg.reasoning_mode == "enabled":
                body["thinking"] = {"type": "enabled"}
            if tools:
                body["tools"] = [{"type": "function", "function": d} for d in definitions]
        return inference_url(cfg), headers, body

    async def complete(self, tools: list[dict]) -> Turn:
        self._last_telemetry = None
        try:
            return await asyncio.wait_for(self._complete_stream(tools), timeout=self.settings.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ModelError("模型流式请求超时；已保留接收内容，未自动重试", telemetry=self._last_telemetry) from exc

    def _progress(self, stream):
        telemetry = extract_telemetry(stream.payload(), self.settings.provider, secrets=(self.settings.api_key,))
        telemetry["usage_final"] = stream.done
        if not stream.done:
            telemetry["reasoning"]["text"] = redact_stream(telemetry["reasoning"]["text"], (self.settings.api_key,))
        self._last_telemetry = telemetry
        if self.on_progress:
            self.on_progress(redact_stream(stream.preview(), (self.settings.api_key,)), telemetry)

    async def _complete_stream(self, tools: list[dict]) -> Turn:
        url, headers, body = self.request(tools)
        stream, decoder = NativeStream(self.settings.provider), SSEDecoder()
        try:
            async with httpx.AsyncClient(timeout=self.settings.timeout_seconds,
                                         follow_redirects=False) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        # Vendor errors often include request bodies and credentials.
                        if response.status_code == 401:
                            raise ModelError("模型服务返回 HTTP 401：推理接口鉴权失败；请检查该渠道的密钥和鉴权协议")
                        if response.status_code == 403:
                            raise ModelError("模型服务返回 HTTP 403：推理请求被拒绝；请检查密钥的模型/接口权限及上游访问策略。模型目录可用不代表推理权限可用")
                        if response.status_code == 429:
                            raise ModelError("模型服务返回 HTTP 429：请求频率或额度受限；请检查上游额度，未自动重试")
                        if response.status_code in {404, 405}:
                            raise ModelError(f"模型服务返回 HTTP {response.status_code}：推理接口路径不存在；请检查 /v1 前缀、路径模式和所选协议")
                        raise ModelError(f"模型服务返回 HTTP {response.status_code}；请检查地址、协议、模型与额度")
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" in content_type:
                        raise ModelError("模型接口返回 HTML 网页，而非 SSE；请检查 /v1 前缀、协议或反向代理路由")
                    if content_type and "text/event-stream" not in content_type:
                        raise ModelError("上游未返回 SSE 流；本项目仅支持流式请求，请检查网关是否支持 stream=true。未自动重试")
                    async for chunk in response.aiter_bytes():
                        for name, data in decoder.feed(chunk):
                            stream.feed(name, data)
                            self._progress(stream)
                            if stream.done: break
                        if stream.done: break
            return self.parse(stream.finish())
        except StreamError as exc:
            raise ModelError(str(exc), telemetry=self._last_telemetry) from exc
        except httpx.TimeoutException as exc:
            raise ModelError("模型流式请求超时；已保留接收内容，未自动重试", telemetry=self._last_telemetry) from exc
        except httpx.HTTPError as exc:
            raise ModelError("模型流式连接中断；已保留接收内容，请检查服务地址与网络，未自动重试", telemetry=self._last_telemetry) from exc
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError) as exc:
            raise ModelError("模型流式字段格式异常；请检查上游是否完整实现所选协议", telemetry=self._last_telemetry) from exc

    def parse(self, response: dict) -> Turn:
        telemetry = extract_telemetry(response, self.settings.provider, secrets=(self.settings.api_key,))
        telemetry["usage_final"] = True
        try:
            turn = self._parse(response)
            turn.telemetry = telemetry
            turn.input_tokens = telemetry["usage"]["input_tokens"] or 0
            turn.output_tokens = telemetry["usage"]["output_tokens"] or 0
            return turn
        except ModelError as exc:
            exc.telemetry = telemetry
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError) as exc:
            raise ModelError("模型响应字段缺失或类型异常；请检查上游是否完整实现所选协议", telemetry=telemetry) from exc

    def _parse(self, response: dict) -> Turn:
        if not isinstance(response, dict):
            raise ModelError("模型响应应为 JSON 对象，实际收到其他结构")
        if response.get("error") or response.get("type") == "error":
            raise ModelError("上游在 HTTP 200 中返回了错误对象；请检查渠道的模型权限、额度和请求参数")
        if response.get("stop_reason") in {"max_tokens", "model_context_window_exceeded"}:
            raise ModelError("模型输出被长度限制截断；请增加输出上限")
        if response.get("status") in {"failed", "incomplete"}:
            raise ModelError("模型未完成输出；请检查输出上限或稍后重试")
        expected = {"anthropic": ("content", "Anthropic Messages"),
                    "openai_responses": ("output", "OpenAI Responses"),
                    "openai_compatible": ("choices", "OpenAI Chat Completions")}
        field_name, label = expected[self.settings.provider]
        if not isinstance(response.get(field_name), list):
            detected = next((name for key, name in expected.values()
                             if key != field_name and isinstance(response.get(key), list)), None)
            if detected:
                raise ModelError(f"协议不匹配：当前选择 {label}，上游返回 {detected} 格式；请修改渠道协议后再测试")
            raise ModelError(f"{label} 响应缺少 {field_name} 数组；请检查模型推理接口，不要使用模型列表接口")
        turn = Turn()
        pending_history = []
        provider = self.settings.provider
        if provider == "anthropic":
            blocks = copy.deepcopy(response["content"])
            pending_history.append({"role": "assistant", "content": blocks})
            for block in blocks:
                if block.get("type") == "text":
                    turn.text += block["text"]
                elif block.get("type") == "tool_use":
                    block["input"] = _arguments(block["input"])
                    turn.calls.append(ToolCall(block["id"], block["name"], block["input"]))
        elif provider == "openai_responses":
            items = response["output"]
            # Preserve reasoning/encrypted reasoning entries for the next turn.
            pending_history.extend(items)
            for item in items:
                if item.get("type") == "function_call":
                    turn.calls.append(ToolCall(item["call_id"], item["name"], _arguments(item["arguments"])))
                elif item.get("type") == "message":
                    turn.text += "\n".join(p.get("text", "") for p in item.get("content", [])
                                            if p.get("type") == "output_text")
        else:
            choice = response["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ModelError("模型输出被长度限制截断；请增加输出上限")
            if choice.get("finish_reason") == "content_filter":
                raise ModelError("上游内容过滤中止了本次输出；已保留用量，未自动重试")
            message = choice["message"]
            saved = {"role": "assistant", "content": message.get("content") or ""}
            if message.get("tool_calls"):
                saved["tool_calls"] = message["tool_calls"]
            # Some compatible reasoning providers require this field on tool turns.
            if "reasoning_content" in message:
                saved["reasoning_content"] = message["reasoning_content"]
            pending_history.append(saved)
            content = saved["content"]
            if isinstance(content, list):
                content = "\n".join(block["text"] for block in content if block.get("type") == "text")
            turn.text = content
            for call in message.get("tool_calls") or []:
                turn.calls.append(ToolCall(call["id"], call["function"]["name"],
                                           _arguments(call["function"]["arguments"])))
        if not isinstance(turn.text, str) or len(turn.text) > 32000 or len(turn.calls) > 4:
            raise ModelError("模型输出超出本轮限制（最多 4 次工具调用）")
        if any(not isinstance(call.id, str) or not isinstance(call.name, str)
               or not call.name or len(call.id) > 200 or len(call.name) > 100 for call in turn.calls):
            raise ModelError("模型工具调用的编号或名称格式异常")
        ids = [call.id for call in turn.calls]
        if len(set(ids)) != len(ids) or any(not value for value in ids):
            raise ModelError("模型工具调用编号无效")
        if not turn.text.strip() and not turn.calls:
            raise ModelError("模型未返回可显示文本或工具调用；可能只有推理内容，请检查输出上限和上游模型状态")
        self.history.extend(pending_history)
        return turn

    def add_results(self, results: list[tuple[ToolCall, dict]]):
        if self.settings.provider == "anthropic":
            self.history.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call.id,
                 "content": json.dumps(result, ensure_ascii=False)} for call, result in results]})
        elif self.settings.provider == "openai_responses":
            self.history.extend({"type": "function_call_output", "call_id": call.id,
                                 "output": json.dumps(result, ensure_ascii=False)} for call, result in results)
        else:
            self.history.extend({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result, ensure_ascii=False)} for call, result in results)
