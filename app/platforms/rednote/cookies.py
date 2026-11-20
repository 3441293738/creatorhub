"""Validate Rednote login snapshots before projecting them into Cookie headers.

Strictly isolated to ``rednote.com`` and ``*.rednote.com`` — domestic
``xiaohongshu.com`` cookies are never accepted into a Rednote session jar.
"""
from __future__ import annotations

import json
import math
import re
import time

from .domains import COOKIE_ROOT

_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_CREATOR_SESSIONS = {
    "galaxy_creator_session_id",
    "access-token-creator.rednote.com",
    "customer-sso-sid",
    "customerClientId",
}


def _domain_allowed(domain: object) -> bool:
    """Allow bare cookie imports (empty domain) or valid ``rednote.com`` hosts."""
    if not isinstance(domain, str):
        return False
    if domain == "":
        return True
    normalized = domain.removeprefix(".").lower()
    return (
        len(normalized) <= 253
        and (normalized == COOKIE_ROOT or normalized.endswith(f".{COOKIE_ROOT}"))
        and all(_LABEL.fullmatch(label) for label in normalized.split("."))
    )


def _pair_valid(name: object, value: object) -> bool:
    return (
        isinstance(name, str)
        and bool(_NAME.fullmatch(name))
        and isinstance(value, str)
        and all(0x21 <= ord(char) <= 0x7E and char not in '\\";,' for char in value)
    )


def _merge(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Deduplicate identical values; drop conflicting values across subdomains."""
    result: dict[str, str] = {}
    conflicts: set[str] = set()
    for name, value in pairs:
        if name in conflicts:
            continue
        if name in result and result[name] != value:
            result.pop(name)
            conflicts.add(name)
        else:
            result[name] = value
    return result


def snapshot_cookies(storage_state_json: str) -> dict[str, str]:
    try:
        state = json.loads(storage_state_json or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(state, dict) or not isinstance(state.get("cookies", []), list):
        return {}
    pairs: list[tuple[str, str]] = []
    now = time.time()
    for cookie in state.get("cookies", []):
        if not isinstance(cookie, dict) or not _domain_allowed(cookie.get("domain", "")):
            continue
        name, value = cookie.get("name"), cookie.get("value")
        if not _pair_valid(name, value):
            continue
        expires = cookie.get("expires")
        if expires is not None:
            if isinstance(expires, bool) or not isinstance(expires, (int, float, str)):
                continue
            try:
                expiry = float(expires)
            except (ValueError, OverflowError):
                continue
            if not math.isfinite(expiry) or (expiry != -1 and expiry <= now):
                continue
        pairs.append((name, value))
    return _merge(pairs)


def cookie_str_from_state(storage_state_json: str) -> str:
    """Project valid ``rednote.com`` cookies into a ``Cookie`` request header."""
    return "; ".join(
        f"{name}={value}"
        for name, value in snapshot_cookies(storage_state_json).items()
    )


def _cookie_map_from_raw(cookie_str: str) -> dict[str, str] | None:
    if not isinstance(cookie_str, str) or any(
        ord(c) < 0x20 or ord(c) == 0x7F for c in cookie_str
    ):
        return None
    pairs: list[tuple[str, str]] = []
    for part in cookie_str.split(";"):
        if not part.strip():
            continue
        name, separator, value = part.strip().partition("=")
        if not separator or not _pair_valid(name, value):
            return None
        pairs.append((name, value))
    return _merge(pairs)


def has_a1(cookie_str: str) -> bool:
    parsed = _cookie_map_from_raw(cookie_str)
    return bool(parsed and parsed.get("a1"))


def has_web_session(storage_state_json: str) -> bool:
    cookies = snapshot_cookies(storage_state_json)
    ws = cookies.get("web_session", "")
    return bool(ws and len(ws) >= 16)


def has_creator_cookies(storage_state_json: str) -> bool:
    """Return True when creator-center session cookies are present on ``rednote.com``."""
    cookies = snapshot_cookies(storage_state_json)
    return any(name in _CREATOR_SESSIONS and bool(value) for name, value in cookies.items())
