"""哔哩哔哩 WBI 参数签名与 Cookie 续期加密工具。

实现细节：
  - 64 项 MIXIN_INDEX 索引表重排后截取前 32 字符作为 `mixin_key`；
  - URL 文件名取最后路径段第一个 `.` 之前的部分，非法 URL 返回空串；
  - 过滤空值参数，值转字符串后剔除 `[!'()*]`，注入秒级时间戳 `wts`；
  - 对齐 WHATWG `URLSearchParams` 表单编码规则（空格转 `+`、`~` 转 `%7E`、`+` 转 `%2B`）；
  - `w_rid = md5(query + mixin_key)`；
  - `get_correspond_path` 使用官方公钥与 RSA-PKCS1-OAEP (SHA-256) 加密 `refresh_{timestamp_ms}`。
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

MIXIN_INDEX: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

VC_COMMON_PARAMS: dict[str, Any] = {
    "build": 0,
    "mobi_app": "web",
    "web_location": "333.40164",
}

BILI_CORRESPOND_PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg\n"
    "Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71\n"
    "nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40\n"
    "JNrRuoEUXpabUzGB8QIDAQAB\n"
    "-----END PUBLIC KEY-----"
)

_WBI_STRIP_RE = re.compile(r"[!'()*]")
_REFRESH_CSRF_RE = re.compile(r'<div id="1-name">([^<]+)</div>')


def url_key(value: str = "") -> str:
    """提取 WBI 图片 URL 末段文件名首个点号前的密钥片段。"""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    tail = (parsed.path or "").split("/")[-1]
    if not tail:
        return ""
    return tail.split(".", 1)[0] or ""


def mixin_key_from_wbi(wbi: Mapping[str, Any]) -> str:
    """拼接 img_url 与 sub_url 片段并按 64 项索引重排后截取 32 位。"""
    if not isinstance(wbi, Mapping):
        raise TypeError("wbi 必须为映射对象")
    joined = f"{url_key(str(wbi.get('img_url') or ''))}{url_key(str(wbi.get('sub_url') or ''))}"
    return "".join(joined[i] if i < len(joined) else "" for i in MIXIN_INDEX)[:32]


def _js_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_js_string(item) for item in value)
    if isinstance(value, Mapping):
        return "[object Object]"
    return str(value)


def whatwg_urlencode(pairs: list[tuple[str, str]]) -> str:
    """对齐 WHATWG URLSearchParams.toString() 的表单编码规则。"""
    encoded: list[str] = []
    for key, val in pairs:
        k_enc = quote(str(key), safe="*-._").replace("%20", "+")
        v_enc = quote(str(val), safe="*-._").replace("%20", "+")
        encoded.append(f"{k_enc}={v_enc}")
    return "&".join(encoded)


def sign_with_key(
    mixin_key: str | Mapping[str, Any],
    params: Mapping[str, Any] | str | None = None,
    *,
    now_ms: int | float | None = None,
    wts: int | None = None,
) -> dict[str, Any]:
    """对参数表执行 WBI 签名，返回包含 `signed_params`、`query`、`w_rid`、`wts` 与 `mixin_key` 的字典。"""
    if isinstance(mixin_key, Mapping) and (params is None or isinstance(params, str)):
        actual_params: Mapping[str, Any] = mixin_key
        actual_key = str(params or "")
    else:
        actual_key = str(mixin_key or "")
        actual_params = params if isinstance(params, Mapping) else {}

    merged: dict[str, Any] = dict(actual_params)
    if wts is not None:
        wts_int = int(wts)
    else:
        ts_ms = time.time() * 1000.0 if now_ms is None else float(now_ms)
        wts_int = int(ts_ms // 1000)
    merged["wts"] = wts_int

    pairs: list[tuple[str, str]] = []
    for key, value in merged.items():
        if value is None:
            continue
        cleaned = _WBI_STRIP_RE.sub("", _js_string(value))
        pairs.append((str(key), cleaned))

    pairs.sort(key=lambda item: item[0])
    query = whatwg_urlencode(pairs)
    w_rid = hashlib.md5((query + actual_key).encode("utf-8")).hexdigest()
    signed_params = {k: v for k, v in pairs}
    signed_params["w_rid"] = w_rid
    return {
        "signed_params": signed_params,
        "query": query,
        "w_rid": w_rid,
        "wts": wts_int,
        "mixin_key": actual_key,
    }


def sign_from_wbi(
    wbi: Mapping[str, Any],
    params: Mapping[str, Any] | None = None,
    *,
    now_ms: int | float | None = None,
    wts: int | None = None,
) -> dict[str, Any]:
    """直接从 `wbi_img` 字典（含 `img_url` / `sub_url`）计算 WBI 签名。"""
    if (
        isinstance(params, Mapping)
        and ("img_url" in params or "sub_url" in params)
        and "img_url" not in wbi
    ):
        wbi, params = params, wbi
    return sign_with_key(mixin_key_from_wbi(wbi), params, now_ms=now_ms, wts=wts)


def get_correspond_path(timestamp_ms: int) -> str:
    """生成 Cookie 续期接口 `/correspond/1/{hex}` 所需的 RSA-OAEP(SHA-256) 路径。"""
    pub_key = serialization.load_pem_public_key(
        BILI_CORRESPOND_PUBLIC_KEY_PEM.encode("ascii")
    )
    plaintext = f"refresh_{int(timestamp_ms)}".encode("utf-8")
    encrypted = pub_key.encrypt(
        plaintext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return encrypted.hex()


def extract_refresh_csrf(html_text: str) -> str:
    """从 `/correspond/1/...` 响应 HTML 中提取 `refresh_csrf`。"""
    match = _REFRESH_CSRF_RE.search(str(html_text or ""))
    return match.group(1).strip() if match else ""
