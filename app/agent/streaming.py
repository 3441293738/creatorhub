"""Bounded SSE decoding and native message assembly; never executes tools.

Transport fragments are previews only. A protocol terminal event is mandatory
before the caller validates the complete native message and its tool arguments.
"""
from __future__ import annotations

import codecs
import copy
import json


class StreamError(Exception):
    pass


class SSEDecoder:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self.buffer, self.lines = "", []
        self.total = self.event_size = 0
        self.skip_lf = False

    def feed(self, chunk: bytes):
        self.total += len(chunk)
        if self.total > 8_000_000:
            raise StreamError("模型流式响应超出总大小限制")
        try:
            self.buffer += self.decoder.decode(chunk)
        except UnicodeError:
            raise StreamError("模型流式响应不是有效 UTF-8") from None
        events = []
        while self.buffer:
            if self.skip_lf:
                self.skip_lf = False
                if self.buffer.startswith("\n"):
                    self.buffer = self.buffer[1:]
            endings = [index for separator in ("\r", "\n") if (index := self.buffer.find(separator)) >= 0]
            if not endings: break
            end = min(endings)
            line, separator, self.buffer = self.buffer[:end], self.buffer[end], self.buffer[end + 1:]
            self.skip_lf = separator == "\r"
            if not line:
                data, name = [], "message"
                for item in self.lines:
                    key, separator, value = item.partition(":")
                    if value.startswith(" "): value = value[1:]
                    if key == "data" and separator: data.append(value)
                    elif key == "event" and separator: name = value
                if data: events.append((name, "\n".join(data)))
                self.lines = []; self.event_size = 0
            elif not line.startswith(":"):
                if not line.startswith(("data:", "event:", "id:", "retry:")):
                    raise StreamError("上游没有返回有效 SSE 流；本项目仅支持流式请求，请检查协议与网关配置")
                self.event_size += len(line)
                if self.event_size > 1_000_000:
                    raise StreamError("模型流式事件超出大小限制")
                self.lines.append(line)
        if len(self.buffer) > 1_000_000:
            raise StreamError("模型流式事件超出大小限制")
        return events


def _index(value, limit=64):
    if type(value) is not int or not 0 <= value < limit:
        raise StreamError("模型流式事件索引无效")
    return value


def _text(value, limit=1_000_000):
    if not isinstance(value, str) or len(value) > limit:
        raise StreamError("模型流式文本字段无效或超出限制")
    return value


def _append(target, key, value, limit):
    target[key] = target.get(key, "") + _text(value, limit)
    if len(target[key]) > limit:
        raise StreamError("模型流式内容超出本轮限制")


def _merge_usage(target, incoming):
    if not isinstance(incoming, dict): return
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            target[key].update(value)
        elif value is not None:
            target[key] = copy.deepcopy(value)


class NativeStream:
    def __init__(self, provider):
        self.provider, self.done, self.started = provider, False, False
        self.usage, self.items, self.blocks, self.calls = {}, {}, {}, {}
        self.closed_blocks, self.arguments = set(), {}
        self.message = {"role": "assistant", "content": ""}
        self.finish_reason = None
        self.response = None
        self.identity = None

    def feed(self, name, data):
        if self.done: return
        if data.strip() == "[DONE]":
            if self.provider != "openai_compatible" or not self.finish_reason:
                raise StreamError("模型流提前结束，未收到完整结束状态；未执行工具")
            self.done = True
            return
        try:
            event = json.loads(data)
        except (ValueError, RecursionError):
            raise StreamError("模型 SSE 事件不是有效 JSON；已保留接收内容，未自动重试") from None
        if not isinstance(event, dict):
            raise StreamError("模型 SSE 事件应为 JSON 对象")
        kind = event.get("type", name)
        if event.get("error") or kind == "error":
            raise StreamError("上游在流中返回错误；已保留接收内容和用量，未自动重试")
        if self.provider == "openai_compatible": self._chat(event)
        elif self.provider == "anthropic": self._anthropic(kind, event)
        else: self._responses(kind, event)

    def _chat(self, event):
        if not isinstance(event.get("choices"), list):
            raise StreamError("流式协议不匹配：需要 Chat Completions choices 数组")
        _merge_usage(self.usage, event.get("usage"))
        choices = event["choices"]
        if not choices: return  # Final usage-only chunk.
        if len(choices) != 1 or choices[0].get("index", 0) != 0:
            raise StreamError("本次仅支持一个流式候选回复")
        choice = choices[0]
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise StreamError("上游未返回流式 delta；请检查网关是否关闭了流式输出")
        if self.finish_reason and any(value for value in delta.values()):
            raise StreamError("模型在结束状态后继续输出内容；未执行工具")
        self.started = True
        if delta.get("content") is not None:
            _append(self.message, "content", delta["content"], 32000)
        for field in ("reasoning_content", "reasoning"):
            if delta.get(field) is not None:
                _append(self.message, field, delta[field], 250000)
        for item in delta.get("tool_calls") or []:
            index = _index(item.get("index"), 4)
            call = self.calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if item.get("id"):
                value = _text(item["id"], 200)
                if call["id"] and call["id"] != value: raise StreamError("流式工具编号发生变化")
                call["id"] = value
            function = item.get("function") or {}
            if function.get("name"):
                value = _text(function["name"], 100)
                if call["function"]["name"] and call["function"]["name"] != value:
                    raise StreamError("流式工具名称发生变化")
                call["function"]["name"] = value
            if function.get("arguments") is not None:
                _append(call["function"], "arguments", function["arguments"], 16000)
        if choice.get("finish_reason") is not None:
            reason = choice["finish_reason"]
            if reason not in {"stop", "tool_calls", "length", "content_filter"}:
                raise StreamError("模型流式结束状态不受支持；未执行工具")
            if self.finish_reason and self.finish_reason != reason: raise StreamError("模型流式结束状态冲突")
            self.finish_reason = reason

    def _anthropic(self, kind, event):
        if kind == "ping": return
        if kind == "message_start":
            if self.started: raise StreamError("模型流重复开始；未执行工具")
            message = event.get("message")
            if not isinstance(message, dict): raise StreamError("Messages 流缺少 message 对象")
            self.started = True
            _merge_usage(self.usage, message.get("usage"))
            return
        if not self.started:
            raise StreamError("流式协议不匹配：需要 Anthropic message_start")
        if kind == "content_block_start":
            index = _index(event.get("index"))
            if index in self.blocks: raise StreamError("模型流重复内容块")
            block = event.get("content_block")
            if not isinstance(block, dict): raise StreamError("模型流内容块无效")
            self.blocks[index] = copy.deepcopy(block)
        elif kind == "content_block_delta":
            index = _index(event.get("index"))
            if index not in self.blocks or index in self.closed_blocks: raise StreamError("模型流内容块顺序异常")
            block, delta = self.blocks[index], event.get("delta", {})
            delta_kind = delta.get("type")
            expected = {"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "thinking", "input_json_delta": "tool_use"}
            if delta_kind in expected and block.get("type") != expected[delta_kind]:
                raise StreamError("模型流增量与内容块类型不匹配；未执行工具")
            if delta_kind == "text_delta" and block.get("type") == "text":
                _append(block, "text", delta["text"], 32000)
            elif delta_kind == "thinking_delta" and block.get("type") == "thinking":
                _append(block, "thinking", delta["thinking"], 250000)
            elif delta_kind == "signature_delta" and block.get("type") == "thinking":
                _append(block, "signature", delta["signature"], 500000)
            elif delta_kind == "input_json_delta" and block.get("type") == "tool_use":
                value = self.arguments.get(index, "") + _text(delta["partial_json"], 16000)
                if len(value) > 16000: raise StreamError("模型工具参数过长")
                self.arguments[index] = value
        elif kind == "content_block_stop":
            index = _index(event.get("index"))
            if index not in self.blocks or index in self.closed_blocks: raise StreamError("模型流内容块结束顺序异常")
            self.closed_blocks.add(index)
        elif kind == "message_delta":
            _merge_usage(self.usage, event.get("usage"))  # Cumulative, never add.
            self.finish_reason = (event.get("delta") or {}).get("stop_reason") or self.finish_reason
        elif kind == "message_stop":
            if not self.finish_reason or len(self.closed_blocks) != len(self.blocks):
                raise StreamError("模型流缺少内容块或消息结束状态；未执行工具")
            self.done = True

    def _responses(self, kind, event):
        if kind in {"response.created", "response.in_progress"}:
            self.started = True
            response = event.get("response") or {}
            self.identity = self.identity or response.get("id")
            _merge_usage(self.usage, response.get("usage"))
        elif kind in {"response.completed", "response.incomplete", "response.failed"}:
            response = event.get("response")
            if not isinstance(response, dict) or not isinstance(response.get("output"), list):
                raise StreamError("Responses 结束事件缺少完整 response.output")
            if self.identity and response.get("id") and response["id"] != self.identity:
                raise StreamError("Responses 流的消息编号发生变化")
            self.response = copy.deepcopy(response)
            self.response["status"] = kind.split(".")[1]
            _merge_usage(self.usage, response.get("usage"))
            self.done = True
        elif kind in {"response.output_item.added", "response.output_item.done"}:
            index = _index(event.get("output_index"))
            item = event.get("item")
            if not isinstance(item, dict): raise StreamError("Responses 流缺少内容项")
            self.items[index] = copy.deepcopy(item)
        elif kind in {"response.output_text.delta", "response.output_text.done", "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done"}:
            index = _index(event.get("output_index"))
            summary = "reasoning_summary" in kind
            part_index = _index(event.get("summary_index" if summary else "content_index"))
            item = self.items.setdefault(index, {"type": "reasoning" if summary else "message"})
            key = "summary" if summary else "content"
            parts = item.setdefault(key, [])
            while len(parts) <= part_index: parts.append({"type": "summary_text" if summary else "output_text", "text": ""})
            if kind.endswith(".delta"):
                _append(parts[part_index], "text", event["delta"], 250000 if summary else 32000)
            else:
                parts[part_index]["text"] = _text(event["text"], 250000 if summary else 32000)
        elif kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            index = _index(event.get("output_index"))
            item = self.items.setdefault(index, {"type": "function_call", "arguments": ""})
            if kind.endswith(".delta"): _append(item, "arguments", event["delta"], 16000)
            else: item["arguments"] = _text(event["arguments"], 16000)
        elif not isinstance(kind, str) or not kind.startswith("response."):
            raise StreamError("流式协议不匹配：需要 OpenAI Responses 事件")
        # Unknown response.* events are deliberately ignored (forward compatible).

    def payload(self):
        if self.provider == "openai_compatible":
            message = {**self.message}
            if self.calls: message["tool_calls"] = [self.calls[index] for index in sorted(self.calls)]
            return {"choices": [{"message": message, "finish_reason": self.finish_reason}], "usage": self.usage}
        if self.provider == "anthropic":
            blocks = []
            for index, item in sorted(self.blocks.items()):
                block = {**item}
                if index in self.arguments: block["input"] = self.arguments[index]
                blocks.append(block)
            return {"content": blocks, "stop_reason": self.finish_reason, "usage": self.usage}
        return self.response or {"output": [self.items[index] for index in sorted(self.items)], "usage": self.usage}

    def preview(self):
        payload = self.payload()
        if self.provider == "openai_compatible":
            text = self.message["content"]
        elif self.provider == "anthropic":
            text = "".join(block.get("text", "") for block in payload["content"] if block.get("type") == "text")
        else:
            text = "".join(part.get("text", "") for item in payload["output"] if item.get("type") == "message"
                           for part in item.get("content", []) if part.get("type") == "output_text")
        if len(text) > 32000:
            raise StreamError("模型流式正文超出本轮限制")
        return text

    def finish(self):
        if not self.done:
            raise StreamError("模型流意外中断，未收到结束事件；已保留部分内容，未执行未完成工具，未自动重试")
        if self.provider == "openai_compatible" and bool(self.calls) != (self.finish_reason == "tool_calls") and self.finish_reason not in {"length", "content_filter"}:
            raise StreamError("模型流的工具调用与结束状态不一致")
        if self.provider == "anthropic":
            has_tools = any(item.get("type") == "tool_use" for item in self.blocks.values())
            if has_tools != (self.finish_reason == "tool_use") and self.finish_reason not in {"max_tokens", "model_context_window_exceeded"}:
                raise StreamError("模型流的工具调用与结束状态不一致")
        result = self.payload()
        if len(json.dumps(result, ensure_ascii=False)) > 1_000_000:
            raise StreamError("模型组装后的响应超出大小限制")
        return result
