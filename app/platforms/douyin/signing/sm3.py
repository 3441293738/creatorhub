"""纯 Python SM3 国密哈希实现。
返回 32 字节摘要 (list[int])，a_bogus 内部多处调用。"""
from __future__ import annotations
from typing import List

_IV = [
    0x7380166f, 0x4914b2b9, 0x172442d7, 0xda8a0600,
    0xa96f30bc, 0x163138aa, 0xe38dee4d, 0xb0fb0e4e,
]


def _rotl(x: int, n: int) -> int:
    n &= 31
    x &= 0xFFFFFFFF
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _p0(x: int) -> int:
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x: int) -> int:
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def _t(j: int) -> int:
    return 0x79CC4519 if j < 16 else 0x7A879D8A


def _ff(x, y, z, j):
    return (x ^ y ^ z) if j < 16 else ((x & y) | (x & z) | (y & z))


def _gg(x, y, z, j):
    return (x ^ y ^ z) if j < 16 else ((x & y) | (~x & z))


def _cf(v: List[int], block: bytes) -> List[int]:
    w = [0] * 68
    for i in range(16):
        w[i] = int.from_bytes(block[i * 4:i * 4 + 4], "big")
    for j in range(16, 68):
        w[j] = (_p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15))
                ^ _rotl(w[j - 13], 7) ^ w[j - 6]) & 0xFFFFFFFF
    w1 = [(w[j] ^ w[j + 4]) & 0xFFFFFFFF for j in range(64)]

    a, b, c, d, e, f, g, h = v
    for j in range(64):
        ss1 = _rotl((_rotl(a, 12) + e + _rotl(_t(j), j)) & 0xFFFFFFFF, 7)
        ss2 = ss1 ^ _rotl(a, 12)
        tt1 = (_ff(a, b, c, j) + d + ss2 + w1[j]) & 0xFFFFFFFF
        tt2 = (_gg(e, f, g, j) + h + ss1 + w[j]) & 0xFFFFFFFF
        d = c
        c = _rotl(b, 9)
        b = a
        a = tt1
        h = g
        g = _rotl(f, 19)
        f = e
        e = _p0(tt2)
    return [(x ^ y) & 0xFFFFFFFF for x, y in zip([a, b, c, d, e, f, g, h], v)]


def sm3_hash(msg: bytes) -> bytes:
    length = len(msg) * 8
    msg = msg + b"\x80"
    while len(msg) % 64 != 56:
        msg += b"\x00"
    msg += length.to_bytes(8, "big")
    v = _IV[:]
    for i in range(0, len(msg), 64):
        v = _cf(v, msg[i:i + 64])
    return b"".join(x.to_bytes(4, "big") for x in v)


def sm3_to_array(data) -> List[int]:
    """对应 SM3ToArray: 返回 32 个 0-255 的字节值。data 可为 str / bytes / list[int]。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    elif isinstance(data, list):
        data = bytes(b & 0xFF for b in data)
    return list(sm3_hash(data))
