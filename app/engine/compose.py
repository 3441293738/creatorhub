"""自动评论文案合成：本地 Spintax 模板渲染 + 多协议大模型文案生成。"""
from __future__ import annotations

import json
import random
import re
from typing import Any, List, Optional

import httpx

from app.agent.config import ConnectionSettings, inference_url
from app.agent.streaming import NativeStream, SSEDecoder, StreamError
from app.ai_copywriting import OVERSEAS_PLATFORMS, PLATFORM_LABELS

# {a|b|c} 形式的同义替换(spintax),不支持嵌套
_SPINTAX = re.compile(r"\{([^{}|]+(?:\|[^{}|]+)+)\}")
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_UNCLOSED = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)
_PREFIX_LABEL = re.compile(r"^(?:评论|回复|文案|输出|Reply|Comment)\s*[:：]\s*", re.IGNORECASE)
_REASONING_TEMP_OMIT = re.compile(r"(?:^|/)(?:o1|o3|o4)(?:[-.]|$)", re.IGNORECASE)


class AiGenerateError(RuntimeError):
    """带错误分类与熔断标记的大模型生成异常（不含任何密钥）。"""

    def __init__(self, message: str, *, category: str = "upstream", fatal: bool = False):
        super().__init__(message)
        self.category = category
        self.fatal = fatal


def parse_templates(templates_json: str) -> List[str]:
    """规则里存的是 JSON 字符串数组;也容忍换行分隔的纯文本。"""
    s = (templates_json or "").strip()
    if not s:
        return []
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return [str(t).strip() for t in v if str(t).strip()]
    except Exception:
        pass
    return [line.strip() for line in s.splitlines() if line.strip()]


def _expand_spintax(text: str, rnd: random.Random) -> str:
    for _ in range(20):
        m = _SPINTAX.search(text)
        if not m:
            break
        choice = rnd.choice(m.group(1).split("|"))
        text = text[:m.start()] + choice + text[m.end():]
    return text


def _fill_vars(text: str, ctx: dict) -> str:
    nick = (ctx.get("nick") or "").strip()
    kw = (ctx.get("kw") or "").strip()
    author = (ctx.get("author") or "").strip()
    work_title = (ctx.get("work_title") or ctx.get("work_desc") or "").strip()[:40]
    platform = PLATFORM_LABELS.get(str(ctx.get("platform") or ""), str(ctx.get("platform") or ""))
    # @对方:仅当模板显式写了 {nick} 才带上,避免无脑 @ 人
    text = text.replace("{nick}", nick)
    text = text.replace("{kw}", kw)
    text = text.replace("{author}", author)
    text = text.replace("{work_title}", work_title)
    text = text.replace("{platform}", platform)
    # 收尾:清掉空 @ 和多余空格
    text = re.sub(r"@(\s|$)", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def render(templates: List[str], ctx: Optional[dict] = None,
           seed: Optional[str] = None, max_chars: int = 200) -> str:
    """从模板库合成一条评论文案。templates 为空时返回空串(调用方应跳过)。"""
    if not templates:
        return ""
    ctx = ctx or {}
    rnd = random.Random(seed) if seed else random
    text = _expand_spintax(rnd.choice(templates), rnd)
    return _fill_vars(text, ctx)[:max(20, int(max_chars or 200))]


def _safe_substitute(template: str, vals: dict[str, str]) -> str:
    """仅替换白名单占位符，避免提示词里出现花括号导致 .format() 静默异常。"""
    result = str(template or "")
    for key, value in vals.items():
        token = "{" + key + "}"
        if token in result:
            result = result.replace(token, value)
    return result


def _language_instruction(platform: str, language_mode: str) -> str:
    mode = (language_mode or "auto").strip().lower()
    if mode == "zh":
        return "请使用自然口语化的中文。"
    if mode == "en":
        return "Write in natural, conversational English."
    if platform in OVERSEAS_PLATFORMS:
        return "请自动匹配对方原文语种（如英文、日文等；原文无明显语种时默认使用自然英文）。"
    return "请使用自然口语化的中文（若对方原文为其他语种，可自然顺应）。"


def _build_messages(ctx: dict[str, Any], ai: dict[str, Any]) -> tuple[str, str]:
    platform = str(ctx.get("platform") or "").strip().lower()
    platform_name = PLATFORM_LABELS.get(platform, platform or "社媒")
    mode = str(ctx.get("mode") or "auto_reply").strip().lower()
    kind_label = "回复" if mode == "auto_reply" else "评论"
    max_chars = max(15, min(300, int(ai.get("max_chars") or 60)))
    language_mode = str(ai.get("language_mode") or "auto")
    lang_hint = _language_instruction(platform, language_mode)

    source_text = str(ctx.get("source_text") or "").strip()[:500]
    work_desc = str(ctx.get("work_desc") or ctx.get("work_title") or "").strip()[:300]
    nick = str(ctx.get("nick") or "").strip()[:60]
    author = str(ctx.get("author") or "").strip()[:60]
    kw = str(ctx.get("kw") or "").strip()[:80]
    recent = [str(x).strip() for x in (ctx.get("recent_replies") or []) if str(x).strip()][-5:]

    vals = {
        "source_text": source_text,
        "work_desc": work_desc,
        "work_title": work_desc[:60],
        "nick": nick,
        "author": author,
        "kw": kw,
        "platform": platform,
        "platform_name": platform_name,
        "kind_label": kind_label,
        "max_chars": str(max_chars),
        "recent_replies": " / ".join(recent),
    }

    system = (
        f"你是{platform_name}创作者的真实互动助手。你只输出最终要发送的一条{kind_label}正文本身。\n"
        f"硬性要求：{lang_hint}长度控制在 {max_chars} 字以内；"
        "不要加引号、书名号、序号、前缀（如「回复：」）、免责声明或任何解释。"
    )

    sections: list[str] = []
    style_prompt = str(ai.get("style_prompt") or "").strip()
    if style_prompt:
        sections.append(f"【人设与风格】\n{_safe_substitute(style_prompt, vals)}")

    custom_prompt = str(ai.get("prompt") or "").strip()
    if custom_prompt:
        sections.append(f"【全局补充要求】\n{_safe_substitute(custom_prompt, vals)}")

    rule_prompt = str(ai.get("rule_prompt") or "").strip()
    if rule_prompt:
        sections.append(f"【当前规则专属要求】\n{_safe_substitute(rule_prompt, vals)}")

    ctx_lines = [
        f"- 平台：{platform_name}",
        f"- 场景：{'回复自己作品下收到的评论' if mode == 'auto_reply' else '在他人作品下发表互动评论'}",
    ]
    if work_desc and work_desc != source_text:
        ctx_lines.append(f"- 作品简介/标题：{work_desc}")
    if source_text:
        ctx_lines.append(f"- {'对方评论原文' if mode == 'auto_reply' else '目标作品文案'}：{source_text}")
    if nick:
        ctx_lines.append(f"- 对方昵称：{nick}")
    if author:
        ctx_lines.append(f"- 作品作者：{author}")
    if kw:
        ctx_lines.append(f"- 触发关键词：{kw}")
    sections.append("【本次互动上下文】\n" + "\n".join(ctx_lines))

    if recent:
        recent_block = "\n".join(f"- {item[:80]}" for item in recent)
        sections.append(
            "【避重参考】以下为近期已生成文案，请换不同的句式与切入点，避免雷同：\n"
            + recent_block
        )

    sections.append(f"请直接输出一条自然真实的{kind_label}：")
    return system, "\n\n".join(sections)


def _clean_generated_text(raw: str, max_chars: int) -> str:
    text = str(raw or "")
    text = _THINK_BLOCK.sub("", text)
    text = _THINK_UNCLOSED.sub("", text)
    text = text.strip()
    # 多行时仅保留非空行并用空格合并，避免模型输出多段解释
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        text = " ".join(lines)
    text = _PREFIX_LABEL.sub("", text).strip()
    text = text.strip('"\'“”‘’「」『』').strip()
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:max(15, int(max_chars or 200))]


def _extract_from_json(provider: str, payload: Any) -> tuple[str, str]:
    """从各协议 JSON 响应提取 (text, finish_or_stop_reason)。"""
    if not isinstance(payload, dict):
        raise AiGenerateError("模型返回结构无效（非 JSON 对象）", category="protocol", fatal=True)
    if payload.get("error") or payload.get("type") == "error":
        err_obj = payload.get("error")
        detail = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj or "")
        raise AiGenerateError(f"上游返回错误：{detail[:160] or '请检查模型权限、额度或参数'}",
                              category="upstream", fatal=False)

    if provider == "anthropic":
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise AiGenerateError("Anthropic 响应缺少 content 数组，请核对渠道协议",
                                  category="protocol", fatal=True)
        text = "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        return text, str(payload.get("stop_reason") or "")

    if provider == "openai_responses":
        output = payload.get("output")
        if not isinstance(output, list):
            raise AiGenerateError("OpenAI Responses 响应缺少 output 数组，请核对渠道协议",
                                  category="protocol", fatal=True)
        parts: list[str] = []
        for item in output:
            if isinstance(item, dict) and item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                        parts.append(str(part.get("text") or ""))
        if not parts and isinstance(payload.get("output_text"), str):
            parts.append(payload["output_text"])
        return "\n".join(parts), str(payload.get("status") or "")

    # openai_compatible
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AiGenerateError("Chat Completions 响应缺少 choices 数组，请核对接口地址与协议",
                              category="protocol", fatal=True)
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    text = str(content or "")
    reason = str(choice.get("finish_reason") or "")
    if not text.strip() and (message.get("reasoning_content") or message.get("reasoning")):
        raise AiGenerateError(
            "模型仅输出了思考过程而未返回最终正文；请调大「最大输出 Token」或改用非推理模型",
            category="empty", fatal=False,
        )
    return text, reason


def _http_status_error(status: int, provider: str) -> AiGenerateError:
    if status == 401:
        return AiGenerateError("模型鉴权失败（HTTP 401），请检查 API Key 与鉴权协议", category="auth", fatal=True)
    if status == 402:
        return AiGenerateError("模型账户余额不足（HTTP 402），请检查上游额度", category="quota", fatal=True)
    if status == 403:
        return AiGenerateError("模型请求被拒绝（HTTP 403），请检查密钥与模型访问权限", category="auth", fatal=True)
    if status in {404, 405}:
        return AiGenerateError(
            f"模型推理路径不存在（HTTP {status}），请检查 /v1 前缀、路径模式与所选协议（{provider}）",
            category="config", fatal=True,
        )
    if status == 429:
        return AiGenerateError("模型触发限流或额度耗尽（HTTP 429）", category="quota", fatal=True)
    if status == 400:
        return AiGenerateError(
            "上游拒绝请求参数（HTTP 400），请检查模型名、输出长度参数（max_tokens / max_completion_tokens）或温度设置",
            category="config", fatal=False,
        )
    return AiGenerateError(f"模型服务返回 HTTP {status}，请检查上游状态或网络", category="upstream", fatal=False)


def _client_kwargs(ai: dict[str, Any]) -> dict[str, Any]:
    timeout = float(ai.get("timeout") or 25)
    proxy_mode = str(ai.get("proxy_mode") or "direct").strip().lower()
    proxy_url = str(ai.get("proxy_url") or "").strip()
    kwargs: dict[str, Any] = {
        "timeout": max(5.0, min(90.0, timeout)),
        "follow_redirects": False,
        "trust_env": proxy_mode == "system",
    }
    if proxy_mode == "custom" and proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


async def generate(ctx: dict[str, Any], ai: dict[str, Any]) -> str:
    """调用大模型 API 生成一条评论文案。支持 OpenAI Compatible / Responses / Anthropic。"""
    provider = str(ai.get("provider") or "openai_compatible").strip().lower()
    if provider not in {"openai_compatible", "openai_responses", "anthropic"}:
        provider = "openai_compatible"
    base_url = str(ai.get("base_url") or "").strip().rstrip("/")
    api_key = str(ai.get("api_key") or "").strip()
    model = str(ai.get("model") or "").strip()
    api_path_mode = str(ai.get("api_path_mode") or "auto").strip().lower()
    if api_path_mode not in {"auto", "exact"}:
        api_path_mode = "auto"

    if not (base_url and model):
        raise AiGenerateError("AI 模型配置不完整（需填写接口地址和模型名称）", category="config", fatal=True)

    try:
        conn = ConnectionSettings(
            provider=provider, base_url=base_url, api_key=api_key, api_path_mode=api_path_mode
        )
    except ValueError as exc:
        raise AiGenerateError(str(exc), category="config", fatal=True) from exc

    url = inference_url(conn)
    system_msg, user_msg = _build_messages(ctx, ai)
    max_tokens = max(64, min(2048, int(ai.get("max_output_tokens") or 256)))
    max_chars = max(15, min(300, int(ai.get("max_chars") or 60)))
    output_param = str(ai.get("output_token_parameter") or "max_tokens")
    if output_param not in {"max_tokens", "max_completion_tokens"}:
        output_param = "max_tokens"

    temp_raw = ai.get("temperature")
    temperature: float | None = None
    if temp_raw not in (None, ""):
        try:
            tval = float(temp_raw)
            if 0.0 <= tval <= 2.0:
                temperature = tval
        except (TypeError, ValueError):
            temperature = None
    if _REASONING_TEMP_OMIT.search(model):
        temperature = None

    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if api_key:
        if provider == "anthropic":
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    body: dict[str, Any] = {"model": model, "stream": False}
    if provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        body["system"] = system_msg
        body["messages"] = [{"role": "user", "content": user_msg}]
        body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
    elif provider == "openai_responses":
        body["instructions"] = system_msg
        body["input"] = [{"role": "user", "content": user_msg}]
        body["max_output_tokens"] = max_tokens
        body["store"] = False
        if temperature is not None:
            body["temperature"] = temperature
    else:
        body["messages"] = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]
        body[output_param] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature

    try:
        async with httpx.AsyncClient(**_client_kwargs(ai)) as cli:
            r = await cli.post(url, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise AiGenerateError("模型请求超时，请检查网络或代理设置", category="network", fatal=False) from exc
    except httpx.HTTPError as exc:
        raise AiGenerateError(f"无法连接模型服务：{exc.__class__.__name__}", category="network", fatal=False) from exc

    if r.status_code != 200:
        raise _http_status_error(r.status_code, provider)

    content_type = (r.headers.get("content-type") or "").lower()
    if "text/html" in content_type:
        raise AiGenerateError("模型接口返回了 HTML 网页，请检查接口地址与 /v1 路径模式",
                              category="protocol", fatal=True)

    if "text/event-stream" in content_type:
        # 兼容部分强制流式返回的中转网关
        try:
            decoder, stream = SSEDecoder(), NativeStream(provider)
            for name, data in decoder.feed(r.content):
                stream.feed(name, data)
                if stream.done:
                    break
            payload = stream.payload()
        except StreamError as exc:
            raise AiGenerateError(f"解析模型流式响应失败：{exc}", category="protocol", fatal=False) from exc
    else:
        try:
            payload = r.json()
        except ValueError as exc:
            raise AiGenerateError("模型返回内容不是有效 JSON，请检查接口地址与协议",
                                  category="protocol", fatal=True) from exc

    raw_text, stop_reason = _extract_from_json(provider, payload)
    text = _clean_generated_text(raw_text, max_chars)
    if not text:
        if stop_reason in {"length", "max_tokens", "incomplete"}:
            raise AiGenerateError("模型输出达到长度上限但未产出正文，请调大最大输出 Token",
                                  category="empty", fatal=False)
        raise AiGenerateError("AI 返回空文案", category="empty", fatal=False)
    return text
