"""Allowlisted tool records still need redaction of free-form error messages."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SECRET_KEY = re.compile(r"cookie|storage_state|authorization|api.?key|token|password|secret|credential", re.I)
_HEADER = re.compile(r"(?im)\b(cookie|authorization)[\"']?\s*[:=]\s*[^\r\n]+")
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|xsec[_-]?token|sessionid|password|secret)"
    r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_URL = re.compile(r"https?://[^\s<>\"']+")


def redact(value, secrets: tuple[str, ...] = ()):
    if isinstance(value, dict):
        return {str(key): "[已脱敏]" if _SECRET_KEY.search(str(key)) else redact(item, secrets)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[已脱敏]")
    value = _HEADER.sub(lambda m: m.group(1) + "=[已脱敏]", value)
    value = _ASSIGNMENT.sub(lambda m: m.group(1) + "=[已脱敏]", value)

    def clean_url(match):
        try:
            url = urlsplit(match.group())
            host = url.hostname or ""
            if ":" in host:
                host = "[" + host + "]"
            if url.port:
                host += ":" + str(url.port)
            return urlunsplit((url.scheme, host, url.path, "", ""))
        except ValueError:
            return "[地址已脱敏]"
    return _URL.sub(clean_url, value)


def redact_stream(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Do not flash a secret prefix while its remaining bytes are in flight."""
    hold = 0
    for secret in secrets:
        if not secret: continue
        for size in range(min(len(text), len(secret) - 1), 0, -1):
            if secret.startswith(text[-size:]):
                hold = max(hold, size)
                break
    if hold: text = text[:-hold]
    # An unfinished URL could later turn its apparent host into userinfo.
    match = re.search(r"https?://[^\s<>\"']*$", text)
    if match: text = text[:match.start()]
    return redact(text, secrets)
