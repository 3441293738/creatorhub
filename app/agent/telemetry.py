"""Allowlisted provider telemetry. Missing usage is unknown, never free usage.

Only public reasoning text/summary is extracted. Signatures, encrypted content
and redacted-thinking payloads stay in the in-memory native provider history.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .privacy import redact

TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens",
                "cached_input_tokens", "cache_write_input_tokens")
REASONING_LIMIT = 16000
RUN_REASONING_LIMIT = 48000


def _object(value):
    return value if isinstance(value, dict) else {}


def _array(value):
    return value if isinstance(value, list) else []


def _count(value):
    if isinstance(value, str) and len(value) <= 13 and value.isascii() and value.isdigit():
        value = int(value)
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _first(*values):
    return next((number for value in values if (number := _count(value)) is not None), None)


def _cost(usage):
    # No universal cost field exists across these protocols. Accept an explicit
    # amount AND currency only; a gateway's scalar cost/credits is not assumed USD.
    cost = usage.get("cost")
    currency = cost.get("currency") if isinstance(cost, dict) else usage.get("cost_currency")
    amount = cost.get("amount") if isinstance(cost, dict) else cost
    if currency not in {"USD", "CNY", "EUR", "GBP", "JPY", "HKD"} or type(amount) not in {int, float, str}:
        return None
    try:
        value = Decimal(str(amount))
        if not value.is_finite() or value < 0 or value > 10**9 or value.as_tuple().exponent < -12:
            return None
        return {"amount": format(value, "f"), "currency": currency, "source": "upstream"}
    except (InvalidOperation, ValueError):
        return None


def extract_telemetry(response, provider, *, secrets=()):
    response = _object(response)
    usage = _object(response.get("usage"))
    input_tokens = _first(usage.get("input_tokens"), usage.get("prompt_tokens"))
    output_tokens = _first(usage.get("output_tokens"), usage.get("completion_tokens"))
    input_details = _object(usage.get("input_tokens_details", usage.get("prompt_tokens_details")))
    output_details = _object(usage.get("output_tokens_details", usage.get("completion_tokens_details")))
    cached = _first(input_details.get("cached_tokens"), usage.get("prompt_cache_hit_tokens"))
    cache_write = None
    if provider == "anthropic":
        cached = _count(usage.get("cache_read_input_tokens"))
        cache_write = _count(usage.get("cache_creation_input_tokens"))
        invalid_cache = any(key in usage and _count(usage[key]) is None for key in
                            ("cache_read_input_tokens", "cache_creation_input_tokens"))
        # Anthropic input_tokens excludes cache reads/writes; OpenAI already
        # includes cached input. Never add cache/reasoning subsets twice.
        input_tokens = (input_tokens + (cached or 0) + (cache_write or 0)
                        if input_tokens is not None and not invalid_cache else None)
    reasoning_tokens = _first(output_details.get("reasoning_tokens"), output_details.get("thinking_tokens"))
    if reasoning_tokens is not None and output_tokens is not None and reasoning_tokens > output_tokens:
        reasoning_tokens = None
    if cached is not None and input_tokens is not None and cached > input_tokens:
        cached = None
    total = _count(usage.get("total_tokens"))
    if total is None and input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens
    normalized = dict(zip(TOKEN_FIELDS, (input_tokens, output_tokens, total, reasoning_tokens, cached, cache_write)))

    parts, length, truncated, protected = [], 0, False, False
    def add(value):
        nonlocal length, truncated
        if not isinstance(value, str) or not value.strip():
            return
        room = max(0, REASONING_LIMIT - length)
        text = redact(value.strip(), secrets)
        if len(text) > room:
            truncated = True
        if room:
            parts.append(text[:room]); length += len(parts[-1]) + 2

    if provider == "openai_responses":
        for item in _array(response.get("output")):
            item = _object(item)
            if item.get("type") == "reasoning":
                for part in _array(item.get("summary")):
                    part = _object(part)
                    if part.get("type") == "summary_text":
                        add(part.get("text"))
                protected |= bool(item.get("encrypted_content"))
    elif provider == "anthropic":
        for block in _array(response.get("content")):
            block = _object(block)
            if block.get("type") == "thinking":
                add(block.get("thinking"))
                protected |= bool(block.get("signature"))
            elif block.get("type") == "redacted_thinking":
                protected = True
    else:
        choices = _array(response.get("choices"))
        message = _object(_object(choices[0]).get("message")) if choices else {}
        value = message.get("reasoning_content")
        add(value if isinstance(value, str) and value.strip() else message.get("reasoning"))
    return {"usage": normalized, "cost": _cost(usage),
            "reasoning": {"text": "\n\n".join(parts), "kind": "summary" if provider == "openai_responses" else "provider_text",
                          "truncated": truncated, "protected": protected}}


def summarize_usage(calls, *, legacy_input=0, legacy_output=0, running=False):
    values = [call.get("usage") or {} for call in calls]
    result = {field: sum(items) if (items := [v[field] for v in values if v.get(field) is not None]) else None
              for field in TOKEN_FIELDS}
    reported = sum(all(v.get(field) is not None for field in TOKEN_FIELDS[:3]) and call.get("usage_final", True)
                   for call, v in zip(calls, values))
    status = "reported" if calls and reported == len(calls) else "partial" if any(v is not None for v in result.values()) else "unavailable"
    if not calls:
        status = "pending" if running else "legacy" if legacy_input or legacy_output else "unavailable"
        if legacy_input or legacy_output:
            result.update(input_tokens=legacy_input, output_tokens=legacy_output, total_tokens=legacy_input + legacy_output)
    costs = {}
    for call in calls:
        if cost := call.get("cost"):
            costs[cost["currency"]] = costs.get(cost["currency"], Decimal(0)) + Decimal(cost["amount"])
    return {**result, "status": status, "requests": len(calls), "reported_requests": reported,
            "costs": [{"currency": key, "amount": format(value, "f"), "source": "upstream"} for key, value in costs.items()],
            "cost_complete": bool(calls) and all(call.get("cost") is not None and call.get("usage_final", True) for call in calls)}
