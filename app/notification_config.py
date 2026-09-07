"""Write-only notification credentials with explicit keep/replace/clear semantics."""
from __future__ import annotations

import json
from urllib.parse import quote, quote_plus, urlsplit

from fastapi import HTTPException

MASK = "********"
_PUBLIC = {"bark": {"server"}, "dingtalk": {"keyword"},
           "telegram": {"chat_id", "api_base"}}


def parse_config(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def sensitive(kind: str, key: str, value) -> bool:
    if key not in _PUBLIC.get(kind, set()):
        return True
    # A malformed/extended public field can itself contain a credential object.
    if value is not None and not isinstance(value, (str, int, float, bool)):
        return True
    if key in {"server", "api_base"}:
        try:
            url = urlsplit(str(value or ""))
            return bool(url.username or url.password or url.query or url.fragment)
        except ValueError:
            return True
    return False


def redact_config(kind: str, config: dict) -> dict:
    return {key: MASK if value not in (None, "") and sensitive(kind, key, value)
            else value for key, value in config.items()}


def merge_config(old: dict, submitted: dict) -> dict:
    # Omitted keys and the mask preserve the newest stored value, including
    # concurrent credential changes made after the edit dialog was opened.
    merged = dict(old)
    for key, value in submitted.items():
        if value == MASK:
            if key not in old or old[key] in (None, ""):
                raise HTTPException(422, f"{key} 尚未配置，请填写真实值")
        else:
            merged[key] = value  # Empty string / null explicitly clears it.
    return merged


def redact_detail(kind: str, config: dict, detail: str) -> str:
    result = str(detail or "")
    hidden = []
    for key, value in config.items():
        if value not in (None, "") and sensitive(kind, key, value):
            raw = str(value)
            hidden.extend([raw, quote(raw, safe=""), quote_plus(raw)])
            # An HTTP client error may echo only the token within a webhook URL.
            if isinstance(value, str) and "://" in value:
                from urllib.parse import parse_qsl
                try:
                    url = urlsplit(value)
                    hidden.extend(v for _, v in parse_qsl(url.query) if v)
                    hidden.extend(v for v in [url.username, url.password] if v)
                except ValueError:
                    pass
    for value in sorted(set(hidden), key=len, reverse=True):
        result = result.replace(value, MASK)
    return result[:800]
