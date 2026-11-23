"""TikTok Web's ``X-Dynosaur`` and ``X-Gnarly``, in pure Python.

TikTok's page loads a bytecode-VM SDK (``webmssdk``, imported as ``acrawler``)
that patches ``window.fetch`` and rewrites the URL of every API call, appending
four query parameters in a fixed order::

    <business query>&X-Dynosaur=<env report>&msToken=<session>&X-Bogus=1&X-Gnarly=<request seal>

This module produces all four without a browser.

The algorithm
-------------
Both parameters share one envelope::

    payload    = TLV entries, each [key][0x00][length][value...]
    key12      = 12 random uint32 drawn per call
    rounds     = 5 + (sum(word & 0xF for word in key12) & 0xF)
    ciphertext = ChaCha-like keystream XOR over the payload as LE uint32 words
    spliced    = ciphertext with the 48-byte key inserted at
                 (sum(key bytes) + sum(ciphertext bytes)) % (len(ciphertext) + 1)
    signature  = custom_base64(b"\\x4b" + spliced)

The signature carries its own key, which lets the server decrypt it. Because
the key changes per call, verification reconstructs a signature with fixed
inputs instead of comparing independently generated tokens.

Verification
------------
Deterministic fixtures decrypt captured signatures, reuse their timestamp,
nonce and encryption key, and compare the reconstructed payload and signature.

* **X-Dynosaur**: the 249-byte plaintext payload is byte-identical, and
  re-encryption reproduces the 400-character signature exactly.
* **X-Gnarly**: the 16-field payload is identical field for field (the SDK emits
  them in an order that varies between processes, so the server must parse by
  key; this module emits ascending). Re-encryption reproduces the 324-character
  signature exactly.

The fixtures cover multiple endpoints, User-Agents, percent-encoded CJK text,
large parameter sets and consecutive call counters.
"""

from __future__ import annotations

import base64
import hashlib
import random
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

# TikTok seals the browser-normalized query, not urllib.parse.urlencode output.
# Chrome leaves printable ASCII alone except for the characters below and
# percent-encodes non-ASCII/control bytes as UTF-8.
_BROWSER_MUST_ESCAPE = {
    " ": "%20", '"': "%22", "<": "%3C",
    ">": "%3E", "`": "%60", "#": "%23",
}


def _browser_escape(text: str) -> str:
    out: list[str] = []
    for ch in str(text):
        if ch in _BROWSER_MUST_ESCAPE:
            out.append(_BROWSER_MUST_ESCAPE[ch])
        elif " " < ch <= "~":
            out.append(ch)
        else:
            out.extend(f"%{byte:02X}" for byte in ch.encode("utf-8"))
    return "".join(out)


def encode_browser_query(pairs: Iterable[tuple[str, str]]) -> str:
    return "&".join(
        f"{_browser_escape(key)}={_browser_escape(value)}"
        for key, value in pairs
    )

MASK32: Final = 0xFFFFFFFF

#: Query parameters, in the order the SDK appends them. The order is the
#: platform's and is not ours to improve: ``X-Gnarly`` seals the query string
#: including ``X-Dynosaur`` and ``msToken``, so anything reordered after signing
#: is a seal over bytes the platform never receives.
DYNOSAUR_PARAM: Final = "X-Dynosaur"
MS_TOKEN_PARAM: Final = "msToken"
BOGUS_PARAM: Final = "X-Bogus"
GNARLY_PARAM: Final = "X-Gnarly"

#: ``X-Bogus`` is the literal one-character string ``1`` on HTTP requests. A
#: computed 16-character value belongs to websocket handshakes and must not be
#: sent on this HTTP request path.
BOGUS_VALUE: Final = "1"

#: Custom base64 alphabet, positional against the standard one. Present verbatim
#: in the SDK's decoded string table (stored there with a trailing ``=`` pad
#: character, which passes through unchanged). Both parameters use this one; the
#: other alphabet in the table belongs to X-Bogus and is not needed.
ALPHABET: Final = "u09tbS3UvgDEe6r-ZVMXzLpsAohTn7mdINQlW412GqBjfYiyk8JORCF5/xKHwacP"
_STANDARD: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_TO_CUSTOM: Final = str.maketrans(_STANDARD, ALPHABET)
_FROM_CUSTOM: Final = str.maketrans(ALPHABET, _STANDARD)

#: First byte of the envelope, before the spliced ciphertext.
ENVELOPE_TAG: Final = 0x4B

#: ChaCha state words 0..3. These differ from the textbook
#: ``expand 32-byte k`` constants.
CHACHA_INIT: Final = (1196819126, 600974999, 3863347763, 1451689750)

#: FNV-1a 32-bit, with a non-standard offset basis and an extra ``* 33`` per
#: byte.
FNV_OFFSET: Final = 2166136260
FNV_PRIME: Final = 16777619

#: Version strings carried by the payload. They are protocol data rather than
#: decoration; a platform version change may require updating the field table.
SDK_VERSION: Final = "5.3.2"
SCM_VERSION: Final = "2.0.0.561"

#: Environment fingerprint the payload reports, taken from a real browser.
#:
#: Incorrect values fail silently on most endpoints. ``/api/post/item_list/``
#: exposes the mismatch by returning HTTP 200 with an empty body and a
#: ``tt_orcas_res: 1`` header.
#:
#: Browser captures report 65 and 8. Synthetic DOM environments may report
#: different values, so these constants must follow browser-observed requests.
ENV_CODE: Final = 65
UB_CODE: Final = 8

#: X-Dynosaur field 0x38. Constant across bundles 2.0.0.514 and 2.0.0.561 and
#: across every environment perturbation tried (UA, screen, location, canvas
#: present/absent), so it is a property of the SDK build rather than of the
#: machine. It is :func:`hash_state` of an internal md5 value. The observed
#: value is stable across the tested environments.
VM_STATE_HASH: Final = 0xC46CE353

#: Canvas fingerprint. ``-1`` means "no canvas". A working 2d context produces
#: a hash instead, so this remains a named constant for per-identity replacement.
CANVAS_HASH: Final = "-1"

#: Three optional environment fields currently use neutral placeholders. Browser
#: requests may report a hash, a component version and an md5 respectively.
WEBGL_HASH: Final = "0"
COMPONENT_VERSION: Final = "0"
DEVICE_HASH: Final = "0"

#: ``location.host + location.pathname`` of the active page. The home page is
#: used by a request client that has no visible page.
PAGE: Final = "www.tiktok.com/"

#: The SDK counts its own signing calls from 1 and puts the counter in four
#: fields. A fresh page signing its first request reports 1.
CALL_SEQUENCE_START: Final = 1

#: md5 of the empty body, which every GET has. A literal in the SDK's own string
#: table, so the SDK special-cases it too.
EMPTY_BODY_MD5: Final = "d41d8cd98f00b204e9800998ecf8427e"

#: The SDK's two byte encoders, as ``(xor_base, add_base, pre_xor, rot, post_add)``.
#: A encodes every field but the checksum; B encodes the checksum and three flags.
#: Public because :func:`decode_field` is the inverse and a reader taking a
#: captured X-Dynosaur apart needs to know which of the two a field used.
ENCODER_A: Final = (103, 1, None, 2, 1)
ENCODER_B: Final = (102, 0, 165, 1, 0)


def encode_query(pairs: Iterable[tuple[str, str]]) -> str:
    """Serialize the business parameters exactly as a browser would send them.

    Signing and transmission must use the same normalized byte sequence.
    """
    return encode_browser_query(pairs)


def hash_state(text: str) -> int:
    """The SDK's FNV-1a variant: the usual round, then ``* 33``."""
    value = FNV_OFFSET
    for byte in text.encode("utf-8"):
        step = ((value ^ byte) * FNV_PRIME) & MASK32
        value = (step + ((step * 32) & MASK32)) & MASK32
    return value


def encode_field(text: str, config: tuple[int, int, int | None, int, int]) -> bytes:
    """One payload field: a per-position byte scramble, padded, length-tagged.

    The output is always at least 6 bytes, ends with ``0x00`` and the original
    length, and pads with ``(221 + index) & 255``. The padding matters: for a
    one-character string byte 1 is always ``0xDE``, which is what makes the
    X-Dynosaur checksum below stable no matter what it is a checksum of.
    """
    xor_base, add_base, pre_xor, rotate, post_add = config
    size = max(len(text) + 2, 6)
    out = bytearray(size)
    for index, char in enumerate(text):
        value = (ord(char) ^ (xor_base + index)) & MASK32
        value = (value + add_base + (170 & index)) % 256
        if pre_xor is not None:
            value ^= pre_xor
        value = ((value << rotate) | (value >> (8 - rotate))) & 0xFF
        out[index] = ((value ^ 187) + post_add) % 256
    for index in range(len(text), size - 2):
        out[index] = (221 + index) & 0xFF
    out[size - 2] = 0
    out[size - 1] = len(text)
    return bytes(out)


def pack_payload(
    fields: Mapping[int, bytes],
    order: Sequence[int] | None = None,
    *,
    lead_count: bool = False,
) -> bytes:
    """Lay the fields out as the TLV entries the platform parses.

    ``order`` exists because X-Gnarly's emission order is the one thing here
    that was inferred rather than measured: the SDK emits its 16 fields in an
    order that is stable within a process and different in the next one, which
    says the server parses by key and cannot be relying on position. Ascending is
    what this module ships; passing an order is how a captured signature can be
    reproduced byte for byte anyway, which is what the unit tests do.
    """
    keys = sorted(fields) if order is None else list(order)
    body = b"".join(bytes((key, 0, len(fields[key]))) + fields[key] for key in keys)
    return bytes((len(keys),)) + body if lead_count else body


def _be(value: int, size: int) -> bytes:
    return int(value).to_bytes(size, "big")


def mix_state(timestamp: int, nonce: int, env_code: int) -> int:
    """Fold two 32-bit values into 16 bits and stamp the environment on top."""
    folded = ((timestamp >> 16) ^ (nonce >> 16) ^ timestamp ^ nonce) & 0xFFFF
    return folded | (env_code << 16)


def fold_checksum(values: Sequence[int | str], mode: int) -> int:
    """XOR fold of the X-Gnarly field values, seeded with all ones.

    Two modes, differing only in how a string contributes: mode 1 treats it as
    zero, mode 2 as its first four UTF-8 bytes big-endian. The seed is why the
    two checksums the SDK emits are complements of the plain folds.
    """
    accumulator = MASK32
    for value in values:
        if isinstance(value, str):
            number = 0 if mode == 1 else int.from_bytes(value.encode("utf-8")[:4], "big")
        else:
            number = value
        accumulator ^= number & MASK32
    return accumulator & MASK32


def _quarter_round(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[a] = (state[a] + state[b]) & MASK32
    state[d] ^= state[a]
    state[d] = ((state[d] << 16) | (state[d] >> 16)) & MASK32
    state[c] = (state[c] + state[d]) & MASK32
    state[b] ^= state[c]
    state[b] = ((state[b] << 12) | (state[b] >> 20)) & MASK32
    state[a] = (state[a] + state[b]) & MASK32
    state[d] ^= state[a]
    state[d] = ((state[d] << 8) | (state[d] >> 24)) & MASK32
    state[c] = (state[c] + state[d]) & MASK32
    state[b] ^= state[c]
    state[b] = ((state[b] << 7) | (state[b] >> 25)) & MASK32


def _keystream(state: Sequence[int], rounds: int) -> list[int]:
    """One 64-byte block. This is NOT ChaCha20 and the differences are load bearing.

    ``rounds`` counts *single* rounds and is data-dependent (5..20), so an odd
    count exits after a column round; getting that exit wrong passes some calls
    and fails others, which looks like flaky rate limiting rather than a bug.
    The diagonal round's third quartet is (2, 7, 12, 13) where the textbook has
    (2, 7, 8, 13) - lane 12 appears twice, which is almost certainly a bug in the
    original and is reproduced deliberately.
    """
    working = list(state)
    done = 0
    while done < rounds:
        _quarter_round(working, 0, 4, 8, 12)
        _quarter_round(working, 1, 5, 9, 13)
        _quarter_round(working, 2, 6, 10, 14)
        _quarter_round(working, 3, 7, 11, 15)
        done += 1
        if done >= rounds:
            break
        _quarter_round(working, 0, 5, 10, 15)
        _quarter_round(working, 1, 6, 11, 12)
        _quarter_round(working, 2, 7, 12, 13)
        _quarter_round(working, 3, 4, 13, 14)
        done += 1
    return [(working[i] + state[i]) & MASK32 for i in range(16)]


def _crypt(key: Sequence[int], rounds: int, payload: bytes) -> bytes:
    """XOR the payload, read as little-endian uint32 words, with the keystream.

    The counter lives in state word 12 and is incremented only after a *full*
    16-word block, so the trailing partial block uses the already-incremented
    state. Payloads here are one block, but the loop is the SDK's.
    """
    state = [*CHACHA_INIT, *(word & MASK32 for word in key)]
    length = len(payload)
    word_count = (length + 3) // 4
    words = [
        int.from_bytes(payload[4 * i : 4 * i + 4].ljust(4, b"\0"), "little")
        for i in range(word_count)
    ]
    offset = 0
    while offset + 16 < word_count:
        block = _keystream(state, rounds)
        state[12] = (state[12] + 1) & MASK32
        for i in range(16):
            words[offset + i] ^= block[i]
        offset += 16
    block = _keystream(state, rounds)
    for i in range(word_count - offset):
        words[offset + i] ^= block[i]
    return b"".join(word.to_bytes(4, "little") for word in words)[:length]


def seal(payload: bytes, key: Sequence[int]) -> str:
    """Encrypt, splice the key back in, and encode. See the module docstring."""
    rounds = (sum(word & 0xF for word in key) & 0xF) + 5
    ciphertext = _crypt(key, rounds, payload)
    key_bytes = b"".join(int(word).to_bytes(4, "little") for word in key)
    position = (sum(key_bytes) + sum(ciphertext)) % (len(ciphertext) + 1)
    spliced = ciphertext[:position] + key_bytes + ciphertext[position:]
    raw = bytes((ENVELOPE_TAG,)) + spliced
    return base64.b64encode(raw).decode("ascii").translate(_TO_CUSTOM)


#: Words in the key spliced into every envelope, as :func:`_random_key` draws it.
KEY_WORDS: Final = 12


def unseal(token: str) -> tuple[bytes, tuple[int, ...]]:
    """Recover ``(payload, key)`` from a signature the platform's own page made.

    The envelope hides its key inside its own ciphertext, at a position derived
    from the key and ciphertext bytes - the server has to recover it the same
    way, so the position is discoverable by testing each candidate against that
    relation. Exactly one satisfies it.

    Use this decoder to validate captured envelopes and their embedded fields.
    Environment constants must be checked against browser-observed requests.
    """
    raw = base64.b64decode(token.translate(_FROM_CUSTOM))
    if not raw or raw[0] != ENVELOPE_TAG:
        raise ValueError("not a signature envelope")
    spliced = raw[1:]
    width = KEY_WORDS * 4
    for position in range(len(spliced) - width + 1):
        key_bytes = spliced[position : position + width]
        ciphertext = spliced[:position] + spliced[position + width :]
        if position != (sum(key_bytes) + sum(ciphertext)) % (len(ciphertext) + 1):
            continue
        key = tuple(
            int.from_bytes(key_bytes[4 * i : 4 * i + 4], "little") for i in range(KEY_WORDS)
        )
        rounds = (sum(word & 0xF for word in key) & 0xF) + 5
        return _crypt(key, rounds, ciphertext), key
    raise ValueError("no splice position satisfies the key relation")


def unpack_payload(payload: bytes, *, lead_count: bool = False) -> dict[int, bytes]:
    """Split a payload back into its TLV fields. The inverse of :func:`pack_payload`."""
    fields: dict[int, bytes] = {}
    offset = 1 if lead_count else 0
    while offset + 3 <= len(payload):
        key, _, length = payload[offset], payload[offset + 1], payload[offset + 2]
        fields[key] = payload[offset + 3 : offset + 3 + length]
        offset += 3 + length
    return fields


def decode_field(raw: bytes, config: tuple[int, int, int | None, int, int]) -> str:
    """Read back a string field. The inverse of :func:`encode_field`."""
    xor_base, add_base, pre_xor, rotate, post_add = config
    out = []
    for index in range(raw[-1]):
        value = ((raw[index] - post_add) % 256) ^ 187
        value = ((value >> rotate) | (value << (8 - rotate))) & 0xFF
        if pre_xor is not None:
            value ^= pre_xor
        value = (value - add_base - (170 & index)) % 256
        out.append(chr(value ^ (xor_base + index)))
    return "".join(out)


def derive_identity_hashes(account_seed: str = "") -> tuple[str, str, str]:
    """Derive deterministic per-account non-zero ``(canvas_hash, webgl_hash, device_hash)``.

    When ``account_seed`` is empty, returns the neutral placeholders
    ``(CANVAS_HASH, WEBGL_HASH, DEVICE_HASH)`` so bare calls stay byte-identical.
    """
    seed = str(account_seed or "").strip()
    if not seed:
        return CANVAS_HASH, WEBGL_HASH, DEVICE_HASH
    digest = hashlib.sha256(f"tiktok-webmssdk-fp|{seed}".encode("utf-8")).digest()
    canvas_u32 = (int.from_bytes(digest[0:4], "big") & 0x7FFFFFFF) or 0x1A2B3C4D
    webgl_u32 = (int.from_bytes(digest[4:8], "big") & 0xFFFFFFFF) or 0x5E6F7A8B
    device_hex = hashlib.md5(f"tiktok-webmssdk-dev|{seed}".encode("utf-8")).hexdigest()
    if int(device_hex, 16) == 0:
        device_hex = "1" + device_hex[1:]
    return str(canvas_u32), str(webgl_u32), device_hex


def dynosaur_payload(
    query: str,
    user_agent: str,
    *,
    timestamp: int,
    nonce: int,
    sequence: int = CALL_SEQUENCE_START,
    env_code: int = ENV_CODE,
    ub_code: int = UB_CODE,
    account_seed: str = "",
    canvas_hash: str | None = None,
    webgl_hash: str | None = None,
    device_hash: str | None = None,
) -> bytes:
    """The environment report, 25 TLV fields with keys 0x20..0x38 ascending.

    Three fields are :func:`hash_state` of something and are the only ones that
    bind the report to anything: 0x2B of the empty string (a GET has no body),
    0x2E of the query - the query alone, not the path - and 0x30 of the
    User-Agent, which is why the signature has to be computed with the same UA
    the request is sent with.
    """
    def_canvas, def_webgl, def_device = derive_identity_hashes(account_seed)
    eff_canvas = def_canvas if canvas_hash is None else str(canvas_hash)
    eff_webgl = def_webgl if webgl_hash is None else str(webgl_hash)
    eff_device = def_device if device_hash is None else str(device_hash)

    fields: dict[int, bytes] = {
        0x21: encode_field("1", ENCODER_B),
        0x22: encode_field("1", ENCODER_B),
        0x23: encode_field("0", ENCODER_A),
        0x24: encode_field(str(mix_state(timestamp, nonce, env_code)), ENCODER_A),
        0x25: encode_field(str(sequence), ENCODER_A),
        0x26: encode_field(str(env_code), ENCODER_A),
        0x27: encode_field(str(timestamp), ENCODER_A),
        0x28: encode_field(eff_webgl, ENCODER_A),
        0x29: encode_field("0", ENCODER_A),
        0x2A: encode_field(SDK_VERSION, ENCODER_A),
        0x2B: _be(hash_state(""), 4),
        0x2C: encode_field(eff_canvas, ENCODER_A),
        0x2D: encode_field("0", ENCODER_A),
        0x2E: _be(hash_state(query), 4),
        0x2F: encode_field(str(sequence), ENCODER_A),
        0x30: _be(hash_state(user_agent), 4),
        0x31: encode_field(SCM_VERSION, ENCODER_A),
        0x32: encode_field(COMPONENT_VERSION, ENCODER_A),
        0x33: encode_field(eff_device, ENCODER_A),
        0x34: encode_field(str(nonce), ENCODER_A),
        0x35: encode_field(PAGE, ENCODER_A),
        0x36: encode_field(str(ub_code), ENCODER_A),
        0x37: encode_field("0", ENCODER_A),
        0x38: _be(VM_STATE_HASH, 4),
        # Placeholder while the checksum below is computed over every field.
        # Any one-character string works, because byte 1 of a one-character
        # field is the constant pad byte; the SDK uses "0" and so do we.
        0x20: encode_field("0", ENCODER_A),
    }
    checksum = 0
    for key in sorted(fields):
        checksum ^= fields[key][1]
    fields[0x20] = encode_field(str(checksum), ENCODER_B)
    return pack_payload(fields)


def gnarly_fields(
    signed_query: str,
    user_agent: str,
    *,
    body: bytes = b"",
    timestamp: int,
    nonce: int,
    nonce2: int,
    sequence: int = CALL_SEQUENCE_START,
    env_code: int = ENV_CODE,
    ub_code: int = UB_CODE,
) -> dict[int, bytes]:
    """The request seal, as a field table. 16 entries; 0x07 is absent.

    ``signed_query`` is the business query with ``&X-Dynosaur=...&msToken=...``
    already appended - the suffix is required even when the token is empty, and
    is what makes this parameter a seal over the whole rewritten URL rather than
    over the caller's parameters alone.

    The two checksums are folded over a fixed logical order that is not the
    emission order. Order does not actually matter to an XOR fold, but keeping
    the SDK's list shape keeps the correspondence readable.
    """
    query_md5 = hashlib.md5(signed_query.encode("utf-8")).hexdigest()
    body_md5 = hashlib.md5(body).hexdigest()
    agent_md5 = hashlib.md5(user_agent.encode("utf-8")).hexdigest()
    mixed = mix_state(timestamp, nonce, env_code)
    covered: list[int | str] = [
        0,
        env_code,
        ub_code,
        query_md5,
        body_md5,
        agent_md5,
        timestamp,
        0,
        nonce,
        SDK_VERSION,
        SCM_VERSION,
        CALL_SEQUENCE_START,
        sequence,
        sequence,
        mixed,
        nonce2,
    ]
    first = fold_checksum(covered, 2)
    second = fold_checksum([*covered, first], 1)
    return {
        0x00: _be(second, 4),
        0x01: _be(env_code, 2),
        0x02: _be(ub_code, 2),
        0x03: query_md5.encode("ascii"),
        0x04: body_md5.encode("ascii"),
        0x05: agent_md5.encode("ascii"),
        0x06: _be(timestamp, 4),
        0x08: _be(nonce, 4),
        0x09: SDK_VERSION.encode("ascii"),
        0x0A: SCM_VERSION.encode("ascii"),
        0x0B: _be(CALL_SEQUENCE_START, 2),
        0x0C: _be(sequence, 2),
        0x0D: _be(sequence, 2),
        0x0E: _be(mixed, 4),
        0x0F: _be(nonce2, 4),
        0x10: _be(first, 4),
    }


def gnarly_payload(
    signed_query: str,
    user_agent: str,
    *,
    body: bytes = b"",
    timestamp: int,
    nonce: int,
    nonce2: int,
    sequence: int = CALL_SEQUENCE_START,
    order: Sequence[int] | None = None,
    env_code: int = ENV_CODE,
    ub_code: int = UB_CODE,
) -> bytes:
    """:func:`gnarly_fields`, packed behind the one-byte field count."""
    fields = gnarly_fields(
        signed_query,
        user_agent,
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        nonce2=nonce2,
        sequence=sequence,
        env_code=env_code,
        ub_code=ub_code,
    )
    return pack_payload(fields, order, lead_count=True)


def _clock_nonce() -> int:
    """A 32-bit value from the microsecond clock, which is what the SDK uses.

    Measured: freezing ``Date.now``, ``performance.now`` and ``Math.random`` made
    both nonces deterministic, and across runs they track wall time at roughly
    one unit per microsecond. Uniform randomness would work equally well as far
    as anything observable goes, but a clock-derived value is what the platform
    has always seen, and it costs nothing to keep.
    """
    return int(time.time() * 1_000_000) & MASK32


def _random_key(rng: random.Random) -> tuple[int, ...]:
    return tuple(rng.getrandbits(32) for _ in range(12))


def sign(
    pairs: Sequence[tuple[str, str]],
    user_agent: str,
    *,
    ms_token: str = "",
    body: bytes = b"",
    timestamp: int | None = None,
    nonce: int | None = None,
    nonce2: int | None = None,
    sequence: int = CALL_SEQUENCE_START,
    account_seed: str = "",
    canvas_hash: str | None = None,
    webgl_hash: str | None = None,
    device_hash: str | None = None,
    rng: random.Random | None = None,
    key: Sequence[int] | None = None,
    key2: Sequence[int] | None = None,
) -> tuple[str, dict[str, str]]:
    """Return ``(query, parameters)`` for one TikTok Web API request.

    ``pairs`` is the business query in the platform's own order; the four
    signature parameters are appended after it and never interleaved. The
    returned query is the exact byte sequence to send - re-encoding it breaks
    the seal.

    ``ms_token`` goes in verbatim, empty included, and must never be fabricated.
    TikTok verifies the token when one is present and accepts its absence, so an
    invented value is strictly worse than none. Measured on one identity,
    2026-09-08: the identity's own token returned 2545 bytes, no token at all
    2541 bytes, and a fabricated token of the same length 0 bytes with
    ``tt_orcas_res: 1``. The same asymmetry explains why an identity minted with
    the 128-character bootstrap token is permanently refused - it holds a real
    token that is not the one it was issued.

    It is inside the sealed bytes, so what is signed and what is sent must be the
    same string.
    """
    source = rng or random.Random()
    stamp = int(time.time()) if timestamp is None else int(timestamp)
    first_nonce = _clock_nonce() if nonce is None else int(nonce)
    second_nonce = (~_clock_nonce()) & MASK32 if nonce2 is None else int(nonce2)

    query = encode_browser_query(pairs)
    dynosaur = seal(
        dynosaur_payload(
            query,
            user_agent,
            timestamp=stamp,
            nonce=first_nonce,
            sequence=sequence,
            account_seed=account_seed,
            canvas_hash=canvas_hash,
            webgl_hash=webgl_hash,
            device_hash=device_hash,
        ),
        key if key is not None else _random_key(source),
    )
    # The seal covers the query as it stands after X-Dynosaur and msToken have
    # been appended, so those two have to be spliced in before X-Gnarly is
    # computed - and X-Bogus, which comes between them on the wire, is not
    # covered. That asymmetry is the platform's.
    sealed_query = f"{query}&{DYNOSAUR_PARAM}={dynosaur}&{MS_TOKEN_PARAM}={ms_token}"
    gnarly = seal(
        gnarly_payload(
            sealed_query,
            user_agent,
            body=body,
            timestamp=stamp,
            nonce=first_nonce,
            nonce2=second_nonce,
            sequence=sequence,
        ),
        key2 if key2 is not None else _random_key(source),
    )
    parameters = {
        DYNOSAUR_PARAM: dynosaur,
        MS_TOKEN_PARAM: ms_token,
        BOGUS_PARAM: BOGUS_VALUE,
        GNARLY_PARAM: gnarly,
    }
    # Emitted raw, not percent-encoded: the alphabet contains `/` and the
    # padding is `=`, and TikTok's own page sends both unescaped.
    signed = f"{sealed_query}&{BOGUS_PARAM}={BOGUS_VALUE}&{GNARLY_PARAM}={gnarly}"
    return signed, parameters


def pick_ms_token(cookies: Mapping[str, str] | None) -> str:
    """The session token from the identity's own jar, or empty like the SDK's."""
    return (cookies or {}).get(MS_TOKEN_PARAM) or ""


__all__ = [
    "ALPHABET",
    "BOGUS_PARAM",
    "BOGUS_VALUE",
    "CALL_SEQUENCE_START",
    "CANVAS_HASH",
    "DEVICE_HASH",
    "DYNOSAUR_PARAM",
    "ENCODER_A",
    "ENCODER_B",
    "ENV_CODE",
    "GNARLY_PARAM",
    "MS_TOKEN_PARAM",
    "SCM_VERSION",
    "SDK_VERSION",
    "UB_CODE",
    "WEBGL_HASH",
    "decode_field",
    "derive_identity_hashes",
    "dynosaur_payload",
    "encode_field",
    "encode_query",
    "fold_checksum",
    "gnarly_fields",
    "gnarly_payload",
    "hash_state",
    "mix_state",
    "pack_payload",
    "pick_ms_token",
    "seal",
    "sign",
    "unpack_payload",
    "unseal",
]
