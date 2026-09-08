"""Validate login snapshots before projecting them into API Cookie headers."""
from __future__ import annotations

import json
import math
import re
import time


_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_CREATOR_SESSIONS = {
    "galaxy_creator_session_id", "access-token-creator.xiaohongshu.com", "customer-sso-sid",
}


def _domain_allowed(domain) -> bool:
    # Old manually imported snapshots may omit domain; retain that format.
    # Explicit domains must be real XHS site domains, not lookalikes or CDN jars.
    if not isinstance(domain, str):
        return False
    if domain == "":
        return True
    domain = domain.removeprefix(".").lower()
    return (len(domain) <= 253
            and (domain == "xiaohongshu.com" or domain.endswith(".xiaohongshu.com"))
            and all(_LABEL.fullmatch(label) for label in domain.split(".")))


def _pair_valid(name, value) -> bool:
    return (isinstance(name, str) and bool(_NAME.fullmatch(name))
            and isinstance(value, str)
            and all(0x21 <= ord(char) <= 0x7e and char not in '\\";,' for char in value))


def _merge(pairs) -> dict[str, str]:
    """Deduplicate equal values, but never choose an arbitrary conflicting login."""
    result = {}
    conflicts = set()
    for name, value in pairs:
        if name in conflicts:
            continue
        if name in result and result[name] != value:
            result.pop(name)
            conflicts.add(name)
        else:
            result[name] = value
    return result


def _snapshot_cookies(storage_state_json: str) -> dict[str, str]:
    try:
        state = json.loads(storage_state_json or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(state, dict) or not isinstance(state.get("cookies", []), list):
        return {}
    pairs = []
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
    """Preserve valid site cookies without injecting extra fields or headers."""
    return "; ".join(f"{name}={value}" for name, value in _snapshot_cookies(storage_state_json).items())


def has_a1(cookie_str: str) -> bool:
    if not isinstance(cookie_str, str) or any(ord(c) < 0x20 or ord(c) == 0x7f for c in cookie_str):
        return False
    pairs = []
    for part in cookie_str.split(";"):
        if not part.strip():
            continue
        name, separator, value = part.strip().partition("=")
        if not separator or not _pair_valid(name, value):
            return False
        pairs.append((name, value))
    return bool(_merge(pairs).get("a1"))


def has_creator_cookies(storage_state_json: str) -> bool:
    """A creator session must be nonempty; a client identifier is not a login."""
    return any(name in _CREATOR_SESSIONS and value
               for name, value in _snapshot_cookies(storage_state_json).items())
