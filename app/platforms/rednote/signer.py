"""Rednote (海外小红书) XYS / XYW request signer and ``rednote.com`` ``b1`` context extractor.

Reconstructs the exact ``XYS_`` / ``XYW_`` / ``x-s-common`` / ``SessionManager``
algorithms (``23492``..``23663``, ``23937``, ``23948``, ``23951``) while fixing
inherited domestic hardcodings for Rednote:
  - Extracts ``b1``, ``dsllt`` (``dslPair``), and ``sc`` (``signCount``) strictly
    from ``*.rednote.com`` ``storage_state`` origins or live ``www.rednote.com/zh``
    ``localStorage`` (never reading ``www.xiaohongshu.com/404``).
  - Synthesizes a coherent Rednote-scoped ``b1`` (with ``location`` set to
    ``https://www.rednote.com/zh`` and cached per account for 300s) when raw
    Cookies are used without browser ``localStorage``.
  - Generates ``x-rap-param`` bound to ``//webapi.rednote.com<uri>`` instead of
    ``//edith.xiaohongshu.com<uri>``.
"""
from __future__ import annotations

import base64
from contextlib import suppress
from dataclasses import dataclass, field
import hashlib
import json
import os
import random
import struct
import threading
import time
from typing import Any, Callable, Dict, Literal, Mapping
from urllib.parse import quote, urlsplit

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .domains import EDIT_DOMAIN, FRONT_HOME_PAGE_URL, is_rednote_host, is_rednote_url
from .formatter import utf16_code_units


def parse_cookie_header(cookies: str | Mapping[str, Any]) -> Dict[str, str]:
    """Parse cookie header per ``23642 SFI185850`` (no percent-decoding, strips outer quotes)."""
    if not isinstance(cookies, str):
        return {str(k): str(v) for k, v in cookies.items()} if isinstance(cookies, Mapping) else {}
    if not cookies.strip():
        return {}
    out: Dict[str, str] = {}
    for part in cookies.split(";"):
        idx = part.find("=")
        if idx == -1:
            continue
        key = part[:idx].strip()
        value = part[idx + 1 :].strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out

SIGN_CONTEXT_TTL_SECONDS = 300.0
SIGN_CONTEXT_RETRY_BACKOFF_SECONDS = 60.0

_STANDARD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CUSTOM_B64 = "ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5"
_X3_B64 = "MfgqrsbcyzPQRStuvC7mn501HIJBo2DEFTKdeNOwxWXYZap89+/A4UVLhijkl63G"

_CUSTOM_B64_TRANS = str.maketrans(_STANDARD_B64, _CUSTOM_B64)
_X3_B64_TRANS = str.maketrans(_STANDARD_B64, _X3_B64)

_XYW_PREFIX = "XYW_"
_XYW_SIGN_SVN = "56"
_XYW_SIGN_TYPE = "x2"
_XYW_SIGN_VERSION = "1"
_XYW_AES_KEY = b"7cc4adla5ay0701v"
_XYW_AES_IV = b"4uzjr7mbsibcaldp"
_XYW_ENV_FLAGS_DEFAULT = "0|0|0|1|0|0|1|0|0|0|1|0|0|0|0|1|0|0|1"

_X3_PREFIX = "mns0301_"
_XYS_PREFIX = "XYS_"
_PAYLOAD_LENGTH = 144
_HASH_IV = (1831565813, 461845907, 2246822507, 3266489909)
_HEX_KEY = bytes.fromhex(
    "71a302257793271ddd273bcee3e4b98d9d7935e1da33f5765e2ea8afb6dc77a5"
    "1a499d23b67c20660025860cbf13d4540d92497f58686c574e508f46e1956344"
    "f39139bf4faf22a3eef120b79258145b2feb5193b6478669961298e79bedca64"
    "6e1a693a926154a5a7a1bd1cf0dedb742f917a747a1e388b234f2277516db711"
    "6035439730fa61e9822a0eca7bff72d8"
)
_VERSION_BYTES = (121, 104, 96, 41)
_A1_LENGTH = 52
_APP_ID_LENGTH = 10
_A3_PREFIX = (2, 97, 51, 16)
_ENV_TABLE = (115, 248, 83, 102, 103, 201, 181, 131, 99, 94, 4, 68, 250, 132, 21)
_ENV_CHECKS_DEFAULT = (0, 1, 18, 1, 0, 0, 0, 0, 0, 0, 3, 0, 0, 0, 0)
_HEX_CHARS = "abcdef0123456789"

_XSCOMMON_TEMPLATE: Dict[str, Any] = {
    "s0": 5,
    "s1": "",
    "x0": "1",
    "x1": "4.3.5",
    "x2": "Windows",
    "x3": "xhs-pc-web",
    "x4": "4.86.0",
    "x5": "",
    "x6": "",
    "x7": "",
    "x8": "",
    "x9": -596800761,
    "x10": 0,
    "x11": "normal",
}

_SIGN_DATA_TEMPLATE: Dict[str, Any] = {
    "x0": "4.3.5",
    "x1": "xhs-pc-web",
    "x2": "Windows",
    "x3": "",
    "x4": "object",
}


def platform_x2_for_ua(user_agent: str) -> str:
    """Derive ``x2`` OS string (`Mac OS`, `Linux`, or `Windows`) from `user_agent`."""
    ua = str(user_agent or "")
    if "Mac OS" in ua or "Macintosh" in ua:
        return "Mac OS"
    if "Linux" in ua and "Android" not in ua:
        return "Linux"
    return "Windows"

# Live www.rednote.com (v4.3.9+) signs both GET and POST endpoints with XYS_.
# Explicit sign_format="xyw" remains available for callers/tests that request it.
XYW_DATA_URIS: frozenset[str] = frozenset()

# Endpoints that attach x-rap-param bound to //webapi.rednote.com<uri>.
XRAP_POST_URIS = frozenset({
    "/api/sns/web/v2/search/notes",
    "/api/sns/web/v1/feed",
    "/web_api/sns/v2/note",
})


def _u32(val: int) -> int:
    return val & 0xFFFFFFFF


def _i32(val: int) -> int:
    u = val & 0xFFFFFFFF
    return u - 0x100000000 if (u & 0x80000000) else u


def _imul32(a: int, b: int) -> int:
    return _i32((_u32(a) * _u32(b)) & 0xFFFFFFFF)


def _rotl32(v: int, n: int) -> int:
    v &= 0xFFFFFFFF
    return ((v << n) | (v >> (32 - n))) & 0xFFFFFFFF


def _js_rem(dividend: int, divisor: int) -> int:
    """Match V8 signed integer remainder ``dividend % divisor``."""
    return dividend - int(dividend / divisor) * divisor


def _little_endian(value: int, length: int = 4) -> list[int]:
    n = int(value)
    out: list[int] = []
    for _ in range(length):
        out.append(n & 0xFF)
        n >>= 8
    return out


def extract_uri(url: str) -> str:
    """Extract canonical URI pathname (`23616`), rejecting empty or root `/`."""
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")
    s = url.strip()
    target = f"https://placeholder.local{s}" if s.startswith("/") else s
    parsed = urlsplit(target)
    if not parsed.scheme or (not s.startswith("/") and not parsed.netloc):
        raise ValueError(f"Cannot extract valid URI path from URL: {s}")
    path = parsed.path or ""
    if not path or path == "/":
        raise ValueError(f"Cannot extract valid URI path from URL: {s}")
    return path


def canonical_request(method: str, path: str, payload: Mapping[str, Any] | None = None) -> str:
    """Serialize canonical request string (`23620` / `23621`)."""
    value: Mapping[str, Any] = payload if payload is not None else {}
    if method.upper() == "POST":
        return path + json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if not value:
        return path
    parts: list[str] = []
    for key, v in value.items():
        if isinstance(v, (list, tuple)):
            text = ",".join("" if item is None else str(item) for item in v)
        elif v is None:
            text = ""
        else:
            text = str(v)
        encoded = quote(text, safe=",")
        parts.append(f"{key}={encoded}")
    return f"{path}?{'&'.join(parts)}"


def get_xt(timestamp: float | None = None, now: Callable[[], float] = time.time) -> int:
    ts = now() if timestamp is None else timestamp
    return int(ts * 1000)


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def ed_xyw_hex(
    normalized_request: str,
    a1: str,
    xt: str,
    env_flags: str | None = None,
) -> str:
    """AES-128-CBC encrypt UTF-8 bytes of Base64(`x1=...;x2=...;x3=...;x4=...;`) (`23657`)."""
    digest = md5_hex(f"url={normalized_request}")
    flags = _XYW_ENV_FLAGS_DEFAULT if env_flags is None else env_flags
    text = f"x1={digest};x2={flags};x3={a1};x4={xt};"
    b64_str = base64.b64encode(text.encode("utf-8")).decode("ascii")
    cipher = AES.new(_XYW_AES_KEY, AES.MODE_CBC, iv=_XYW_AES_IV)
    encrypted = cipher.encrypt(pad(b64_str.encode("utf-8"), AES.block_size))
    return encrypted.hex()


def common_checksum(b1: str | bytes) -> int:
    """Compute `x9` signed 32-bit CRC accumulator over low code-unit bytes (`23659`/`23663`)."""
    crc = 0xFFFFFFFF
    if isinstance(b1, str):
        raw = [ord(ch) & 0xFF for ch in b1]
    else:
        raw = list(bytes(b1))
    for value in raw:
        t = (crc & 0xFF) ^ value
        for _ in range(8):
            t = ((t >> 1) ^ 3988292384) if (t & 1) else (t >> 1)
        crc = _u32(t ^ (crc >> 8))
    return _i32(0xFFFFFFFF ^ crc ^ 3988292384)


def encode_custom_b64(data: str | bytes | bytearray, table: dict[int, int] = _CUSTOM_B64_TRANS) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    return base64.b64encode(raw).decode("ascii").translate(table)


def encode_x3(data: bytes | bytearray | list[int]) -> str:
    return encode_custom_b64(bytes(data), _X3_B64_TRANS)


def direction(user_id: str | None, rng: Callable[[], float] = random.random) -> int:
    """MurmurHash3-32 sharding key preserving V8 signed remainder (`23619`)."""
    if user_id is None:
        return int(rng() * (100 - 10 + 1)) + 10
    b = user_id.encode("utf-8")
    h = 151488
    full_blocks = len(b) // 4
    for i in range(full_blocks):
        (k,) = struct.unpack_from("<i", b, i * 4)
        k = _imul32(k, 3432918353)
        k = _rotl32(k, 15)
        k = _imul32(k, 461845907)
        h = _i32(h ^ k)
        h = _i32(_imul32(_rotl32(h, 13), 5) + 3864292196)
    off = full_blocks * 4
    rem = len(b) % 4
    k = 0
    if rem >= 3:
        k ^= b[off + 2] << 16
    if rem >= 2:
        k ^= b[off + 1] << 8
    if rem >= 1:
        k ^= b[off]
        k = _imul32(k, 3432918353)
        k = _rotl32(k, 15)
        k = _imul32(k, 461845907)
        h = _i32(h ^ k)
    h = _u32(h ^ len(b))
    h = _u32(h ^ (h >> 16))
    h = _u32(_imul32(h, 2246822507))
    h = _u32(h ^ (h >> 13))
    h = _u32(_imul32(h, 3266489909))
    h_signed = _i32(h ^ (h >> 16))
    return _js_rem(h_signed, 100) + 1


def custom_hash_v2(input_bytes: bytes | bytearray | list[int]) -> list[int]:
    """16-byte hash mixer with recovered IEEE-754 IV (`23606`)."""
    a, b, c, d = (_u32(x) for x in _HASH_IV)
    raw = bytes(input_bytes)
    n = len(raw)
    a = _u32(a ^ n)
    b = _u32(b ^ ((n << 8) & 0xFFFFFFFF))
    c = _u32(c ^ ((n << 16) & 0xFFFFFFFF))
    d = _u32(d ^ ((n << 24) & 0xFFFFFFFF))
    for i in range(n // 8):
        x, y = struct.unpack_from("<II", raw, i * 8)
        a = _rotl32(_u32(_u32(a + x) ^ c), 7)
        b = _rotl32(_u32((x ^ b) + d), 11)
        c = _rotl32(_u32(_u32(c + y) ^ a), 13)
        d = _rotl32(_u32((d ^ y) + b), 17)
    t1 = _u32(a ^ n)
    t2 = _u32(b ^ t1)
    t3 = _u32(c + t2)
    t4 = _u32(d ^ t3)
    a = _u32(_rotl32(t1, 9) + _rotl32(t3, 17))
    b = _u32(_rotl32(t2, 13) ^ _rotl32(t4, 19))
    c = _u32(_rotl32(t3, 17) + a)
    d = _u32(_rotl32(t4, 19) ^ b)
    out: list[int] = []
    for word in (a, b, c, d):
        out.extend(_little_endian(word, 4))
    return out


def xor_transform_array(input_bytes: list[int] | bytes) -> bytes:
    """Finite-key XOR transform (`23581` / `23582`) without repeating key modulus."""
    key_len = len(_HEX_KEY)
    return bytes(
        ((val & 0xFF) ^ (_HEX_KEY[idx] if idx < key_len else 0)) & 0xFF
        for idx, val in enumerate(input_bytes)
    )


@dataclass
class RednoteSessionState:
    page_load_timestamp: int
    sequence_value: int
    window_props_length: int
    uri_length: int


class RednoteSessionManager:
    """Stateful per-account XYS sequence/window tracker (`23612` / `23613` / `23614`)."""

    def __init__(
        self,
        *,
        now_ms: Callable[[], int] | None = None,
        rng: Callable[[], float] = random.random,
    ):
        self._rng = rng
        now_fn = now_ms or (lambda: int(time.time() * 1000))
        self.page_load_timestamp = int(now_fn())
        self.sequence_value = int(rng() * (17 - 15 + 1)) + 15
        self.window_props_length = int(rng() * (2000 - 1000 + 1)) + 1000

    def update_state(self) -> None:
        self.sequence_value += int(self._rng() * 2)
        self.window_props_length += int(self._rng() * 10) + 1

    def get_current_state(self, normalized_request: str) -> RednoteSessionState:
        self.update_state()
        return RednoteSessionState(
            page_load_timestamp=self.page_load_timestamp,
            sequence_value=self.sequence_value,
            window_props_length=self.window_props_length,
            uri_length=utf16_code_units(normalized_request),
        )


def build_payload_array(
    full_md5: str,
    path_or_full_md5: str,
    a1: str,
    app_id: str = "xhs-pc-web",
    normalized_request: str = "",
    timestamp: float | None = None,
    state: RednoteSessionState | None = None,
    *,
    now: Callable[[], float] = time.time,
    rng: Callable[[], float] = random.random,
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> list[int]:
    """Build 144-byte XYS binary payload (`23609`)."""
    ts = now() if timestamp is None else timestamp
    (nonce,) = struct.unpack("<I", random_bytes(4))
    nonce_low = nonce & 0xFF
    result: list[int] = [*_VERSION_BYTES, *_little_endian(nonce, 4)]
    timestamp_bytes = _little_endian(int(ts * 1000), 8)
    result.extend(timestamp_bytes)
    if state is not None:
        result.extend(_little_endian(state.page_load_timestamp, 8))
        result.extend(_little_endian(state.sequence_value, 4))
        result.extend(_little_endian(state.window_props_length, 4))
        result.extend(_little_endian(state.uri_length, 4))
    else:
        offset = int(rng() * (50 - 10 + 1)) + 10
        result.extend(_little_endian(int((ts - offset) * 1000), 8))
        result.extend(_little_endian(int(rng() * (50 - 15 + 1)) + 15, 4))
        result.extend(_little_endian(int(rng() * (1200 - 1000 + 1)) + 1000, 4))
        result.extend(_little_endian(len(normalized_request.encode("utf-8")), 4))

    digest = bytes.fromhex(full_md5)
    for i in range(8):
        result.append(digest[i] ^ nonce_low)

    a1_raw = a1.encode("utf-8")[:_A1_LENGTH]
    a1_padded = a1_raw + b"\x00" * (_A1_LENGTH - len(a1_raw))
    result.append(_A1_LENGTH)
    result.extend(a1_padded)

    app_raw = app_id.encode("utf-8")[:_APP_ID_LENGTH]
    app_padded = app_raw + b"\x00" * (_APP_ID_LENGTH - len(app_raw))
    result.append(_APP_ID_LENGTH)
    result.extend(app_padded)

    result.append(1)
    result.append(nonce_low ^ _ENV_TABLE[0])
    for i in range(1, 15):
        result.append(_ENV_TABLE[i] ^ _ENV_CHECKS_DEFAULT[i])

    second = list(bytes.fromhex(path_or_full_md5))
    mixed = custom_hash_v2([*timestamp_bytes, *second])
    result.extend(_A3_PREFIX)
    result.extend((byte ^ nonce_low) & 0xFF for byte in mixed)
    return result


@dataclass
class RednoteSignContext:
    """Extracted or synthesized ``rednote.com`` browser sign material (`23948`)."""

    b1: str = ""
    dsl_pair: str = ""
    sign_count: int | None = None
    user_id: str = ""
    user_agent: str = ""
    source: str = "none"
    updated_at: float = field(default_factory=time.time)

    def next_sign_count(self) -> int | None:
        if self.sign_count is None:
            return None
        current = int(self.sign_count)
        self.sign_count = current + 1
        return current


_SIGN_CTX_CACHE: Dict[str, RednoteSignContext] = {}
_SESSION_CACHE: Dict[str, RednoteSessionManager] = {}
_CACHE_LOCK = threading.Lock()


def clear_rednote_sign_caches() -> None:
    with _CACHE_LOCK:
        _SIGN_CTX_CACHE.clear()
        _SESSION_CACHE.clear()


def _session_for_key(key: str) -> RednoteSessionManager:
    with _CACHE_LOCK:
        mgr = _SESSION_CACHE.get(key)
        if mgr is None:
            mgr = RednoteSessionManager()
            _SESSION_CACHE[key] = mgr
        return mgr


def extract_sign_context_from_state(storage_state: str | Mapping[str, Any] | None) -> RednoteSignContext:
    """Extract ``b1``, ``dsllt`` (``dslPair``), and ``sc`` (``signCount``) strictly from ``rednote.com`` origins."""
    if not storage_state:
        return RednoteSignContext()
    if isinstance(storage_state, str):
        try:
            parsed = json.loads(storage_state)
        except Exception:
            return RednoteSignContext()
    elif isinstance(storage_state, Mapping):
        parsed = storage_state
    else:
        return RednoteSignContext()

    origins = parsed.get("origins")
    if not isinstance(origins, list):
        return RednoteSignContext()

    b1 = ""
    dsl_pair = ""
    sign_count: int | None = None

    # Sort so www.rednote.com takes priority over secondary subdomains, while
    # rejecting every non-rednote.com origin (especially xiaohongshu.com).
    valid_origins: list[Mapping[str, Any]] = []
    for entry in origins:
        if not isinstance(entry, Mapping):
            continue
        origin_url = str(entry.get("origin") or "").strip()
        if not is_rednote_url(origin_url):
            continue
        valid_origins.append(entry)

    valid_origins.sort(
        key=lambda item: 0 if "www.rednote.com" in str(item.get("origin") or "").lower() else 1
    )

    for entry in valid_origins:
        ls = entry.get("localStorage")
        if not isinstance(ls, list):
            continue
        kv: dict[str, str] = {}
        for pair in ls:
            if isinstance(pair, Mapping) and pair.get("name"):
                kv[str(pair["name"])] = str(pair.get("value") or "")
        if not b1 and kv.get("b1"):
            b1 = kv["b1"].strip()
        if not dsl_pair:
            cand = (kv.get("dsllt") or kv.get("dslPair") or "").strip()
            if cand and cand != "0":
                dsl_pair = cand
        if sign_count is None and "sc" in kv:
            with suppress(ValueError, TypeError):
                sign_count = int(float(kv["sc"]))

    source = "storage_state" if (b1 or dsl_pair or sign_count is not None) else "none"
    return RednoteSignContext(
        b1=b1,
        dsl_pair=dsl_pair,
        sign_count=sign_count,
        source=source,
    )


def synthesize_rednote_b1(
    cookies: Mapping[str, str],
    *,
    user_agent: str = "",
    locale: str = "en-US",
    timezone: str = "UTC",
) -> str:
    """Synthesize a Rednote-coherent ``b1`` (`23651` -> `23655`) without domestic URLs."""
    from xhshow.config import CryptoConfig
    from xhshow.generators.fingerprint import FingerprintGenerator

    cfg = CryptoConfig()
    ua = str(user_agent or cfg.PUBLIC_USERAGENT).strip()
    x2 = platform_x2_for_ua(ua)
    cfg = cfg.with_overrides(
        PUBLIC_USERAGENT=ua,
        SIGNATURE_DATA_TEMPLATE={**cfg.SIGNATURE_DATA_TEMPLATE, "x2": x2},
        SIGNATURE_XSCOMMON_TEMPLATE={**cfg.SIGNATURE_XSCOMMON_TEMPLATE, "x2": x2},
    )
    gen = FingerprintGenerator(cfg)
    fp = gen.generate(cookies=dict(cookies), user_agent=ua)
    fp["x1"] = ua
    fp["x3"] = str(locale or "en-US")
    fp["x12"] = str(timezone or "UTC")
    fp["x66"] = {
        "referer": "",
        "location": FRONT_HOME_PAGE_URL,
        "frame": 0,
    }
    return gen.generate_b1(fp)


def resolve_rednote_sign_context(
    cookie_str: str,
    *,
    storage_state: str | Mapping[str, Any] | None = None,
    explicit_context: RednoteSignContext | None = None,
    user_agent: str = "",
    locale: str = "en-US",
    timezone: str = "UTC",
) -> RednoteSignContext:
    """Return cached or freshly built ``RednoteSignContext`` (300s TTL per ``a1``)."""
    if explicit_context is not None and explicit_context.b1:
        if user_agent and not explicit_context.user_agent:
            explicit_context.user_agent = user_agent
        return explicit_context

    cookies = parse_cookie_header(cookie_str)
    a1 = str(cookies.get("a1") or "").strip()
    from_state = extract_sign_context_from_state(storage_state)
    cache_key = f"{a1}:{locale}:{user_agent[:48]}"
    now = time.time()

    with _CACHE_LOCK:
        cached = _SIGN_CTX_CACHE.get(cache_key)
        if from_state.b1:
            ctx = RednoteSignContext(
                b1=from_state.b1,
                dsl_pair=from_state.dsl_pair or (cached.dsl_pair if cached else ""),
                sign_count=(
                    from_state.sign_count
                    if from_state.sign_count is not None
                    else (cached.sign_count if cached and cached.sign_count is not None else 0)
                ),
                user_agent=user_agent,
                source="storage_state",
                updated_at=now,
            )
            _SIGN_CTX_CACHE[cache_key] = ctx
            return ctx
        if cached is not None and (now - cached.updated_at) < SIGN_CONTEXT_TTL_SECONDS:
            if from_state.dsl_pair and not cached.dsl_pair:
                cached.dsl_pair = from_state.dsl_pair
            if user_agent and not cached.user_agent:
                cached.user_agent = user_agent
            return cached

    b1 = synthesize_rednote_b1(
        cookies,
        user_agent=user_agent,
        locale=locale,
        timezone=timezone,
    )
    ctx = RednoteSignContext(
        b1=b1,
        dsl_pair=from_state.dsl_pair,
        sign_count=from_state.sign_count if from_state.sign_count is not None else 0,
        user_agent=user_agent,
        source="synthesized_rednote_fp" if not from_state.dsl_pair else "hybrid_state_fp",
        updated_at=now,
    )
    with _CACHE_LOCK:
        _SIGN_CTX_CACHE[cache_key] = ctx
    return ctx


async def refresh_rednote_sign_context(
    mgr: Any,
    identity: Any,
    storage_state_json: str = "",
    *,
    timeout_ms: int = 8000,
) -> tuple[RednoteSignContext, str]:
    """Read live ``b1`` / ``dsllt`` / ``sc`` from ``https://www.rednote.com/zh`` (`23948` fixed for Rednote).

    Returns ``(sign_context, updated_storage_state_json)``. On page failure,
    retains previous cached context with a 60s retry window (`now - 300 + 60`).
    """
    cookie_str = ""
    if storage_state_json:
        from .cookies import cookie_str_from_state

        cookie_str = cookie_str_from_state(storage_state_json)
    cookies = parse_cookie_header(cookie_str)
    a1 = str(cookies.get("a1") or getattr(identity, "key", "") or "anon")
    locale = str(getattr(identity, "locale", "") or "en-US")
    ua = str(getattr(identity, "ua", "") or "")
    cache_key = f"{a1}:{locale}:{ua[:48]}"
    now = time.time()

    with _CACHE_LOCK:
        old = _SIGN_CTX_CACHE.get(cache_key)
        if old is not None and old.source == "live_browser" and (now - old.updated_at) < SIGN_CONTEXT_TTL_SECONDS:
            return old, storage_state_json

    page = None
    created = False
    try:
        ctx = await mgr.context_for(identity)
        for candidate in getattr(ctx, "pages", ()) or ():
            cur_url = str(getattr(candidate, "url", "") or "")
            if is_rednote_url(cur_url) and not candidate.is_closed():
                page = candidate
                break
        if page is None:
            page = await ctx.new_page()
            created = True
            await page.goto(
                FRONT_HOME_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=max(2000, int(timeout_ms)),
            )
        raw_ls = await page.evaluate(
            """() => ({
                b1: window.localStorage.getItem('b1') || '',
                dsllt: window.localStorage.getItem('dsllt') || '',
                sc: window.localStorage.getItem('sc')
            })"""
        )
        new_state_dict = await ctx.storage_state()
        # Ensure non-rednote origins are never persisted into Rednote state.
        if isinstance(new_state_dict.get("origins"), list):
            new_state_dict["origins"] = [
                o for o in new_state_dict["origins"]
                if isinstance(o, Mapping) and is_rednote_url(str(o.get("origin") or ""))
            ]
        if isinstance(new_state_dict.get("cookies"), list):
            new_state_dict["cookies"] = [
                c for c in new_state_dict["cookies"]
                if isinstance(c, Mapping) and is_rednote_host(str(c.get("domain") or "").lstrip("."))
            ]
        updated_state_json = json.dumps(new_state_dict, ensure_ascii=False)
        b1_val = str((raw_ls or {}).get("b1") or (old.b1 if old else "") or "").strip()
        if not b1_val:
            b1_val = synthesize_rednote_b1(cookies, user_agent=ua, locale=locale)
        dsllt_val = str((raw_ls or {}).get("dsllt") or (old.dsl_pair if old else "") or "").strip()
        sc_raw = (raw_ls or {}).get("sc")
        sc_val = old.sign_count if old else 0
        if sc_raw is not None and str(sc_raw).strip() != "":
            with suppress(ValueError, TypeError):
                sc_val = int(float(sc_raw))
        refreshed = RednoteSignContext(
            b1=b1_val,
            dsl_pair=dsllt_val,
            sign_count=sc_val,
            user_agent=ua,
            source="live_browser",
            updated_at=now,
        )
        with _CACHE_LOCK:
            _SIGN_CTX_CACHE[cache_key] = refreshed
        return refreshed, updated_state_json
    except Exception:
        fallback = resolve_rednote_sign_context(
            cookie_str,
            storage_state=storage_state_json,
            user_agent=ua,
            locale=locale,
        )
        fallback.updated_at = now - SIGN_CONTEXT_TTL_SECONDS + SIGN_CONTEXT_RETRY_BACKOFF_SECONDS
        with _CACHE_LOCK:
            _SIGN_CTX_CACHE[cache_key] = fallback
        return fallback, storage_state_json
    finally:
        if created and page is not None:
            with suppress(Exception):
                await page.close()


def generate_rednote_x_rap_param(uri: str, data: Mapping[str, Any] | str | bytes) -> str:
    """Generate ``x-rap-param`` bound to ``//webapi.rednote.com<uri>`` (never ``edith.xiaohongshu.com``)."""
    from xhshow.core.xrap import x_rap_param

    rap_uri = extract_uri(uri)
    rap_api = f"//{EDIT_DOMAIN}{rap_uri}"
    return x_rap_param(rap_api, data if data is not None else {})


class RednoteSigner:
    """Pure-Python Rednote ``XYS_`` / ``XYW_`` / ``x-s-common`` (`b1`) signer."""

    def __init__(
        self,
        *,
        user_agent: str = "",
        now: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ):
        self.user_agent = user_agent
        self.now = now
        self.rng = rng
        self.random_bytes = random_bytes

    def get_xt(self, timestamp: float | None = None) -> int:
        return get_xt(timestamp, self.now)

    def sign_xyw(
        self,
        method: str,
        url: str,
        a1: str,
        app_id: str = "xhs-pc-web",
        payload: Mapping[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> str:
        normalized = canonical_request(method, extract_uri(url), payload)
        xt = str(self.get_xt(timestamp))
        ciphertext = ed_xyw_hex(normalized, a1, xt)
        obj = {
            "signSvn": _XYW_SIGN_SVN,
            "signType": _XYW_SIGN_TYPE,
            "appId": app_id,
            "signVersion": _XYW_SIGN_VERSION,
            "payload": ciphertext,
        }
        raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return _XYW_PREFIX + base64.b64encode(raw).decode("ascii")

    def sign_xs(
        self,
        method: str,
        url: str,
        a1: str,
        app_id: str = "xhs-pc-web",
        payload: Mapping[str, Any] | None = None,
        timestamp: float | None = None,
        session: RednoteSessionManager | None = None,
        *,
        user_agent: str = "",
    ) -> str:
        path = extract_uri(url)
        normalized = canonical_request(method, path, payload)
        full_digest = md5_hex(normalized)
        second_digest = full_digest if method.upper() == "GET" else md5_hex(path)
        state = session.get_current_state(normalized) if session is not None else None
        payload_bytes = build_payload_array(
            full_digest,
            second_digest,
            a1,
            app_id,
            normalized,
            timestamp,
            state,
            now=self.now,
            rng=self.rng,
            random_bytes=self.random_bytes,
        )
        transformed = xor_transform_array(payload_bytes)[:_PAYLOAD_LENGTH]
        inner = _X3_PREFIX + encode_x3(transformed)
        x2 = platform_x2_for_ua(user_agent or self.user_agent)
        obj = {**_SIGN_DATA_TEMPLATE, "x2": x2, "x3": inner}
        return _XYS_PREFIX + encode_custom_b64(
            json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        )

    def common_object(
        self,
        cookies: Mapping[str, str] | str,
        *,
        b1: str,
        dsl_pair: str = "",
        sign_count: int | None = None,
        user_agent: str = "",
    ) -> Dict[str, Any]:
        parsed = parse_cookie_header(cookies) if isinstance(cookies, str) else dict(cookies)
        if "a1" not in parsed:
            raise ValueError("Missing 'a1' in cookies")
        if not isinstance(b1, str) or not b1:
            raise ValueError("Non-empty 'b1' is required for x-s-common")
        x2 = platform_x2_for_ua(user_agent or self.user_agent)
        obj: Dict[str, Any] = {
            **_XSCOMMON_TEMPLATE,
            "x2": x2,
            "x5": parsed["a1"],
            "x8": b1,
            "x9": common_checksum(b1),
        }
        if sign_count is not None and not isinstance(sign_count, bool):
            obj["x10"] = int(sign_count)
        if dsl_pair:
            obj["x12"] = str(dsl_pair)
        return obj

    def sign_xs_common(
        self,
        cookies: Mapping[str, str] | str,
        *,
        b1: str,
        dsl_pair: str = "",
        sign_count: int | None = None,
        user_agent: str = "",
    ) -> str:
        obj = self.common_object(
            cookies,
            b1=b1,
            dsl_pair=dsl_pair,
            sign_count=sign_count,
            user_agent=user_agent,
        )
        return encode_custom_b64(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))

    def get_b3_trace_id(self) -> str:
        return "".join(_HEX_CHARS[int(self.rng() * 16)] for _ in range(16))

    def get_xray_trace_id(
        self,
        timestamp_ms: int | None = None,
        sequence: int | None = None,
    ) -> str:
        ts_ms = int(self.now() * 1000) if timestamp_ms is None else int(timestamp_ms)
        seq = int(self.rng() * 8388608) if sequence is None else int(sequence)
        part1 = f"{((ts_ms << 23) | seq):016x}"[-16:]
        part2 = "".join(_HEX_CHARS[int(self.rng() * 16)] for _ in range(16))
        return part1 + part2

    def sign_headers(
        self,
        method: Literal["GET", "POST"],
        uri: str,
        cookies: Mapping[str, str] | str,
        *,
        xsec_appid: str = "xhs-pc-web",
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        timestamp: float | None = None,
        session: RednoteSessionManager | None = None,
        sign_format: Literal["xys", "xyw"] | None = None,
        user_id: str | None = None,
        x_rap: bool | None = None,
        b1: str = "",
        dsl_pair: str = "",
        sign_count: int | None = None,
        user_agent: str = "",
    ) -> Dict[str, str]:
        upper = method.upper()
        if upper == "GET":
            if payload is not None:
                raise ValueError("GET requests must use 'params', not 'payload'")
            chosen = params
        elif upper == "POST":
            if params is not None:
                raise ValueError("POST requests must use 'payload', not 'params'")
            chosen = payload
        else:
            raise ValueError(f"Unsupported method: {method}")

        path = extract_uri(uri)
        fmt = sign_format or ("xyw" if upper == "GET" and path in XYW_DATA_URIS else "xys")
        if fmt not in ("xys", "xyw"):
            raise ValueError(f"Unsupported sign_format={fmt!r}")
        use_xrap = (upper == "POST" and path in XRAP_POST_URIS) if x_rap is None else bool(x_rap)

        parsed = parse_cookie_header(cookies) if isinstance(cookies, str) else dict(cookies)
        a1 = str(parsed.get("a1") or "")
        if not a1:
            raise ValueError("Missing 'a1' in cookies")

        eff_ua = user_agent or self.user_agent
        ts = self.now() if timestamp is None else timestamp
        sess = session if session is not None else _session_for_key(a1)
        resolved_b1 = b1
        if not resolved_b1:
            ctx = resolve_rednote_sign_context(
                "; ".join(f"{k}={v}" for k, v in parsed.items()),
                user_agent=eff_ua,
            )
            resolved_b1 = ctx.b1
            if not dsl_pair:
                dsl_pair = ctx.dsl_pair
            if sign_count is None:
                sign_count = ctx.next_sign_count()

        if fmt == "xyw":
            xs = self.sign_xyw(upper, uri, a1, xsec_appid, chosen, ts)
        else:
            xs = self.sign_xs(
                upper, uri, a1, xsec_appid, chosen, ts, sess, user_agent=eff_ua
            )

        xs_common = self.sign_xs_common(
            parsed,
            b1=resolved_b1,
            dsl_pair=dsl_pair,
            sign_count=sign_count,
            user_agent=eff_ua,
        )
        xt = self.get_xt(ts)
        headers: Dict[str, str] = {
            "x-s": xs,
            "x-s-common": xs_common,
            "x-t": str(xt),
            "x-b3-traceid": self.get_b3_trace_id(),
            "x-xray-traceid": self.get_xray_trace_id(int(ts * 1000)),
            "x-mns": "unload",
            "xy-direction": str(direction(user_id, self.rng)),
        }
        if use_xrap:
            headers["x-rap-param"] = generate_rednote_x_rap_param(path, chosen or {})
        return headers


__all__ = [
    "RednoteSessionManager",
    "RednoteSessionState",
    "RednoteSignContext",
    "RednoteSigner",
    "SIGN_CONTEXT_TTL_SECONDS",
    "XRAP_POST_URIS",
    "XYW_DATA_URIS",
    "build_payload_array",
    "canonical_request",
    "clear_rednote_sign_caches",
    "common_checksum",
    "custom_hash_v2",
    "direction",
    "ed_xyw_hex",
    "encode_custom_b64",
    "encode_x3",
    "extract_sign_context_from_state",
    "extract_uri",
    "generate_rednote_x_rap_param",
    "get_xt",
    "platform_x2_for_ua",
    "refresh_rednote_sign_context",
    "resolve_rednote_sign_context",
    "synthesize_rednote_b1",
    "xor_transform_array",
]
