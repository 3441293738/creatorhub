"""纯 Python RC4 流加密工具。
a_bogus 用它加密中间字节串。返回 list[int] (0-255)。"""
from __future__ import annotations
from typing import List, Union


def rc4(key: Union[bytes, List[int]], data: Union[bytes, List[int]]) -> List[int]:
    if isinstance(key, str):
        key = key.encode("latin1")
    if isinstance(data, str):
        data = data.encode("latin1")
    key = list(key)
    data = list(data)

    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]

    out: List[int] = []
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        k = s[(s[i] + s[j]) & 0xFF]
        out.append(byte ^ k)
    return out
