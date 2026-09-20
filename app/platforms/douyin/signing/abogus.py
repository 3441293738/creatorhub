"""Pure-Python generator for Douyin's current ``a_bogus`` query signature.

The wire format follows the 1.0.1.19 algorithm used by the current web client.
It keeps the public ``ABogus``/``sign_url`` API stable for the rest of CreatorHub.

Algorithm structure derived from Johnserf-Seed/f2 ``f2.utils.abogus``
(Apache-2.0), with the repository's existing pure-Python SM3 and RC4 helpers.
"""
from __future__ import annotations

import random
import time
import urllib.parse
from typing import Iterable, List

from .rc4 import rc4
from .sm3 import sm3_to_array

_ALPHABETS = (
    "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe",
    "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe",
)
_OPTIONS = (0, 1, 14)
_SALT = "cus"
_SORT_INDEX = (
    18, 20, 52, 26, 30, 34, 58, 38, 40, 53, 42, 21, 27, 54, 55, 31, 35,
    57, 39, 41, 43, 22, 28, 32, 60, 36, 23, 29, 33, 37, 44, 45, 59, 46,
    47, 48, 49, 50, 24, 25, 65, 66, 70, 71,
)
_XOR_INDEX = (
    18, 20, 26, 30, 34, 38, 40, 42, 21, 27, 31, 35, 39, 41, 43, 22, 28,
    32, 36, 23, 29, 33, 37, 44, 45, 46, 47, 48, 49, 50, 24, 25, 52, 53,
    54, 55, 57, 58, 59, 60, 65, 66, 70, 71,
)
_TRANSFORM_BOX = (
    121, 243, 55, 234, 103, 36, 47, 228, 30, 231, 106, 6, 115, 95, 78,
    101, 250, 207, 198, 50, 139, 227, 220, 105, 97, 143, 34, 28, 194,
    215, 18, 100, 159, 160, 43, 8, 169, 217, 180, 120, 247, 45, 90, 11,
    27, 197, 46, 3, 84, 72, 5, 68, 62, 56, 221, 75, 144, 79, 73, 161,
    178, 81, 64, 187, 134, 117, 186, 118, 16, 241, 130, 71, 89, 147,
    122, 129, 65, 40, 88, 150, 110, 219, 199, 255, 181, 254, 48, 4, 195,
    248, 208, 32, 116, 167, 69, 201, 17, 124, 125, 104, 96, 83, 80, 127,
    236, 108, 154, 126, 204, 15, 20, 135, 112, 158, 13, 1, 188, 164,
    210, 237, 222, 98, 212, 77, 253, 42, 170, 202, 26, 22, 29, 182, 251,
    10, 173, 152, 58, 138, 54, 141, 185, 33, 157, 31, 252, 132, 233,
    235, 102, 196, 191, 223, 240, 148, 39, 123, 92, 82, 128, 109, 57,
    24, 38, 113, 209, 245, 2, 119, 153, 229, 189, 214, 230, 174, 232,
    63, 52, 205, 86, 140, 66, 175, 111, 171, 246, 133, 238, 193, 99,
    60, 74, 91, 225, 51, 76, 37, 145, 211, 166, 151, 213, 206, 0, 200,
    244, 176, 218, 44, 184, 172, 49, 216, 93, 168, 53, 21, 183, 41, 67,
    85, 224, 155, 226, 242, 87, 177, 146, 70, 190, 12, 162, 19, 137,
    114, 25, 165, 163, 192, 23, 59, 9, 94, 179, 107, 35, 7, 142, 131,
    239, 203, 149, 136, 61, 249, 14, 156,
)


def _custom_b64(data: Iterable[int], alphabet: str) -> str:
    raw = bytes(x & 0xFF for x in data)
    out: List[str] = []
    for offset in range(0, len(raw), 3):
        chunk = raw[offset:offset + 3]
        value = int.from_bytes(chunk.ljust(3, b"\0"), "big")
        out.append(alphabet[(value >> 18) & 63])
        out.append(alphabet[(value >> 12) & 63])
        out.append(alphabet[(value >> 6) & 63] if len(chunk) > 1 else "=")
        out.append(alphabet[value & 63] if len(chunk) > 2 else "=")
    return "".join(out)


def _double_digest(value: str) -> List[int]:
    return sm3_to_array(sm3_to_array(value + _SALT))


def _ua_digest(user_agent: str) -> List[int]:
    encrypted = rc4(bytes(_OPTIONS), user_agent.encode("utf-8"))
    encoded = _custom_b64(encrypted, _ALPHABETS[1])
    return sm3_to_array(encoded)


def _random_bytes(groups: int = 3) -> List[int]:
    out: List[int] = []
    for _ in range(groups):
        value = int(random.random() * 10000)
        out.extend((
            ((value & 0xFF) & 0xAA) | 1,
            ((value & 0xFF) & 0x55) | 2,
            ((value >> 8) & 0xAA) | 5,
            ((value >> 8) & 0x55) | 40,
        ))
    return out


def _transform(values: Iterable[int]) -> List[int]:
    box = list(_TRANSFORM_BOX)
    index_b = box[1]
    initial = value_e = 0
    out: List[int] = []
    for index, value in enumerate(values):
        if index == 0:
            initial = box[index_b]
            mixed_index = index_b + initial
            box[1] = initial
            box[index_b] = index_b
        else:
            mixed_index = initial + value_e
        mixed_index %= len(box)
        out.append((value & 0xFF) ^ box[mixed_index])

        slot = (index + 2) % len(box)
        value_e = box[slot]
        mixed_index = (index_b + value_e) % len(box)
        initial = box[mixed_index]
        box[mixed_index], box[slot] = box[slot], initial
        index_b = mixed_index
    return out


class ABogus:
    def __init__(self, user_agent: str = "Mozilla/5.0", fp: str = ""):
        self.ua = user_agent
        self.fp = fp or (
            "1536|747|1560|827|0|0|0|0|1536|864|1536|864|1536|747|24|24|"
            "Win32"
        )

    def get_value(self, params: str, body: str = "") -> str:
        started = int(time.time() * 1000)
        params_hash = _double_digest(params)
        body_hash = _double_digest(body)
        ua_hash = _ua_digest(self.ua)
        finished = int(time.time() * 1000)

        fields = {
            8: 3,
            18: 44,
            19: [1, 0, 1, 0, 1],
            66: 0,
            69: 0,
            70: 0,
            71: 0,
        }
        fields.update({
            20: (started >> 24) & 0xFF,
            21: (started >> 16) & 0xFF,
            22: (started >> 8) & 0xFF,
            23: started & 0xFF,
            24: (started >> 32) & 0xFF,
            25: (started >> 40) & 0xFF,
            26: (_OPTIONS[0] >> 24) & 0xFF,
            27: (_OPTIONS[0] >> 16) & 0xFF,
            28: (_OPTIONS[0] >> 8) & 0xFF,
            29: _OPTIONS[0] & 0xFF,
            30: (_OPTIONS[1] >> 8) & 0xFF,
            31: _OPTIONS[1] & 0xFF,
            32: (_OPTIONS[1] >> 24) & 0xFF,
            33: (_OPTIONS[1] >> 16) & 0xFF,
            34: (_OPTIONS[2] >> 24) & 0xFF,
            35: (_OPTIONS[2] >> 16) & 0xFF,
            36: (_OPTIONS[2] >> 8) & 0xFF,
            37: _OPTIONS[2] & 0xFF,
            38: params_hash[21],
            39: params_hash[22],
            40: body_hash[21],
            41: body_hash[22],
            42: ua_hash[23],
            43: ua_hash[24],
            44: (finished >> 24) & 0xFF,
            45: (finished >> 16) & 0xFF,
            46: (finished >> 8) & 0xFF,
            47: finished & 0xFF,
            48: fields[8],
            49: (finished >> 32) & 0xFF,
            50: (finished >> 40) & 0xFF,
            51: 0,
            52: 0,
            53: 0,
            54: 0,
            55: 0,
            56: 6383,
            57: 6383 & 0xFF,
            58: (6383 >> 8) & 0xFF,
            59: (6383 >> 16) & 0xFF,
            60: (6383 >> 24) & 0xFF,
            64: len(self.fp),
            65: len(self.fp),
        })

        ordered = [fields.get(index, 0) for index in _SORT_INDEX]
        checksum = fields[_XOR_INDEX[0]]
        for index in _XOR_INDEX[1:]:
            checksum ^= fields.get(index, 0)
        payload = ordered + list(self.fp.encode("ascii")) + [checksum & 0xFF]
        return _custom_b64(_random_bytes() + _transform(payload), _ALPHABETS[0])


def sign_url(query_string: str, user_agent: str, body: str = "",
             fp: str = "") -> str:
    """Return a query string carrying a current, URL-escaped ``a_bogus``."""
    signature = ABogus(user_agent=user_agent, fp=fp).get_value(
        query_string, body)
    separator = "&" if query_string else ""
    return (f"{query_string}{separator}a_bogus="
            f"{urllib.parse.quote(signature, safe='')}")
