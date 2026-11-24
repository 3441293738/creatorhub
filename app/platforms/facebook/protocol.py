"""Facebook 协议层原语：Cookie / StorageState 解析、公共主页 `i_user` 身份切换、请求头构造与文案校验。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
import time
from typing import Any, Dict, List, Mapping, Tuple

DEFAULT_FACEBOOK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Facebook 已登录态核心 Cookie：`c_user`（数字 UID）+ `xs`（会话凭证）
FACEBOOK_LOGIN_COOKIES = ("c_user", "xs")
# 切换公共主页（Profile+ Page）操作身份时使用的 Cookie 键
FACEBOOK_PAGE_COOKIE = "i_user"

_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SPINTAX_RE = re.compile(r"\{[^{}]*\|[^{}]*\}", re.DOTALL)
_C_USER_RE = re.compile(r"\bc_user\s*=\s*(\d{4,25})\b")
_XS_RE = re.compile(r"\bxs\s*=\s*([^\s;\"']+)")
_I_USER_RE = re.compile(r"\bi_user\s*=\s*(\d{4,25})\b")


def _is_facebook_cookie_domain(domain: str) -> bool:
    d = (domain or "").strip().lower().lstrip(".")
    if not d:
        return True
    return (
        d in {"facebook.com", "messenger.com", "fb.com"}
        or d.endswith(".facebook.com")
        or d.endswith(".messenger.com")
    )


def _merge_cookie_items(items: List[Any]) -> Dict[str, str]:
    secondary: Dict[str, str] = {}
    primary: Dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        domain = str(item.get("domain") or "").strip().lower()
        if not name or not value or not _is_facebook_cookie_domain(domain):
            continue
        if "facebook.com" in domain or not domain:
            primary[name] = value
        else:
            secondary[name] = value
    merged = dict(secondary)
    merged.update(primary)
    return merged


def parse_facebook_credential_input(raw: str) -> Tuple[Dict[str, str], str]:
    """兼容解析多种 Facebook 登录凭证输入，返回 `(cookies_dict, hint_label)`。

    支持格式：
    1. 浏览器完整 `Cookie` 字符串（`c_user=...; xs=...; datr=...; sb=...; fr=...`）
    2. JSON Cookie 数组 `[{"name":"c_user","value":"..."}, ...]`
    3. Patchright / Playwright `storage_state` JSON `{"cookies": [...]}`
    4. 扁平 JSON 对象 `{"c_user": "...", "xs": "..."}`
    5. 多字段账号文本（如 `uid|pass|2fa|c_user=...;xs=...` 或 `email----pass----cookie`）
    """
    text = str(raw or "").strip()
    if not text:
        return {}, ""

    # 1) JSON 数组 / 对象 / storage_state
    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            cmap = _merge_cookie_items(parsed)
            return cmap, str(cmap.get("c_user") or "")
        if isinstance(parsed, Mapping):
            if isinstance(parsed.get("cookies"), list):
                cmap = _merge_cookie_items(parsed["cookies"])
                return cmap, str(cmap.get("c_user") or "")
            flat = {
                str(k).strip(): str(v).strip()
                for k, v in parsed.items()
                if isinstance(v, (str, int)) and str(k).strip() and str(v).strip()
            }
            if flat:
                return flat, str(flat.get("c_user") or "")

    # 2) 标准 Cookie 头字符串（允许带 `Cookie:` 前缀）
    body = re.sub(r"^\s*cookie\s*:\s*", "", text, flags=re.IGNORECASE)
    out: Dict[str, str] = {}
    hint = ""

    # 若包含多字段分隔符（`----` 或 `|`），优先找含 `c_user=` 的段
    cookie_seg = body
    if "----" in body or "|" in body:
        parts = re.split(r"----|\|", body)
        for idx, seg in enumerate(parts):
            s = seg.strip()
            if "c_user=" in s and "xs=" in s:
                cookie_seg = s
                if idx > 0 and parts[0].strip() and "=" not in parts[0]:
                    hint = parts[0].strip()[:40]
                break

    if "=" in cookie_seg:
        for part in re.split(r";|\r?\n", cookie_seg):
            k, sep, v = part.strip().partition("=")
            k_clean = k.strip()
            v_clean = v.strip().strip('"')
            if sep and _COOKIE_NAME_RE.fullmatch(k_clean) and v_clean:
                out[k_clean] = v_clean

    # 3) 兜底正则提取 c_user / xs / i_user
    if not out.get("c_user"):
        m_uid = _C_USER_RE.search(text)
        if m_uid:
            out["c_user"] = m_uid.group(1)
    if not out.get("xs"):
        m_xs = _XS_RE.search(text)
        if m_xs:
            out["xs"] = m_xs.group(1)
    if not out.get("i_user"):
        m_iu = _I_USER_RE.search(text)
        if m_iu:
            out["i_user"] = m_iu.group(1)

    if not hint:
        hint = str(out.get("c_user") or "")
    return out, hint


def build_facebook_storage_state(cookies: Mapping[str, str]) -> str:
    """将 Facebook Cookie 字典编码为持久化 `storage_state` JSON（覆盖 `.facebook.com`）。"""
    expires = int(time.time()) + 365 * 86400
    items: List[Dict[str, Any]] = []
    http_only_keys = {"xs", "datr", "sb", "fr", "spin", "ps_l", "ps_n"}
    for raw_k, raw_v in (cookies or {}).items():
        name = str(raw_k or "").strip()
        value = str(raw_v or "").strip()
        if not name or not value:
            continue
        items.append(
            {
                "name": name,
                "value": value,
                "domain": ".facebook.com",
                "path": "/",
                "expires": expires,
                "httpOnly": name in http_only_keys,
                "secure": True,
                "sameSite": "None",
            }
        )
    return json.dumps({"cookies": items, "origins": []}, ensure_ascii=False)


def build_page_switch_cookie(page_id: str) -> Dict[str, Any]:
    """构造用于切换 Facebook 公共主页操作身份的 `i_user` Cookie 条目。"""
    return {
        "name": FACEBOOK_PAGE_COOKIE,
        "value": str(page_id or "").strip(),
        "domain": ".facebook.com",
        "path": "/",
        "expires": int(time.time()) + 30 * 86400,
        "httpOnly": False,
        "secure": True,
        "sameSite": "None",
    }


def cookie_map_from_state(storage_state: str | Mapping[str, Any] | None) -> Dict[str, str]:
    """从 Patchright `storage_state` JSON 或 Cookie 文本提取 Facebook 会话 Cookie 字典。"""
    if not storage_state:
        return {}
    if isinstance(storage_state, str):
        cmap, _ = parse_facebook_credential_input(storage_state)
        return cmap
    if not isinstance(storage_state, Mapping):
        return {}
    cookies = storage_state.get("cookies")
    if isinstance(cookies, list):
        return _merge_cookie_items(cookies)
    return {
        str(k).strip(): str(v).strip()
        for k, v in storage_state.items()
        if isinstance(v, (str, int)) and str(k).strip() and str(v).strip()
    }


def cookie_str_from_state(storage_state: str | Mapping[str, Any] | None) -> str:
    cmap = cookie_map_from_state(storage_state)
    return "; ".join(f"{k}={v}" for k, v in cmap.items() if k and v)


def has_facebook_login_cookies(storage_state: str | Mapping[str, Any] | None) -> bool:
    """校验是否同时具备 Facebook 核心登录凭证 `c_user` 与 `xs`。"""
    cmap = cookie_map_from_state(storage_state)
    c_user = str(cmap.get("c_user") or "").strip()
    xs = str(cmap.get("xs") or "").strip()
    return bool(c_user and c_user.isdigit() and xs)


def extract_c_user_uid(cookies: Mapping[str, str]) -> str:
    raw = str((cookies or {}).get("c_user") or "").strip()
    return raw if raw.isdigit() else ""


def extract_i_user_page_id(cookies: Mapping[str, str]) -> str:
    raw = str((cookies or {}).get(FACEBOOK_PAGE_COOKIE) or "").strip()
    return raw if raw.isdigit() else ""


def build_facebook_web_headers(
    cookies: Mapping[str, str],
    *,
    user_agent: str = "",
    referer: str = "https://www.facebook.com/",
    accept_language: str = "en-US,en;q=0.9",
    include_cookie: bool = True,
) -> Dict[str, str]:
    """构造与账号浏览器画像一致的 Facebook Web 文档请求头。"""
    ua = (user_agent or DEFAULT_FACEBOOK_UA).strip()
    m = re.search(r"Chrome/(\d+)", ua)
    major = int(m.group(1)) if m else 131
    brands = (
        f'"Chromium";v="{major}", "Microsoft Edge";v="{major}", "Not?A_Brand";v="99"'
        if "Edg/" in ua
        else f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not?A_Brand";v="99"'
    )
    platform = (
        '"macOS"'
        if ("Mac OS" in ua or "Macintosh" in ua)
        else '"Linux"'
        if ("Linux" in ua and "Android" not in ua)
        else '"Windows"'
    )
    headers: Dict[str, str] = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": accept_language or "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": referer or "https://www.facebook.com/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": brands,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
        "viewport-width": "1920",
        "dpr": "1",
    }
    if include_cookie:
        cookie_header = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items() if k and v)
        if cookie_header:
            headers["Cookie"] = cookie_header
    return headers


def compute_totp(
    secret_key: str,
    *,
    timestamp: int | None = None,
    digits: int = 6,
    period: int = 30,
) -> str:
    """标准 RFC 6238 TOTP 生成器（供 Facebook 2FA 密钥辅助校验）。"""
    cleaned = re.sub(r"[\s\-]+", "", str(secret_key or "")).upper()
    if not cleaned:
        raise ValueError("empty TOTP secret")
    pad = (-len(cleaned)) % 8
    key = base64.b32decode(cleaned + ("=" * pad), casefold=True)
    counter = int(time.time() if timestamp is None else timestamp) // max(1, int(period))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code:0{digits}d}"


def parse_members_count(members_text: str) -> int:
    """解析群组/主页成员数文本（支持英文 K/M 与中文万/亿单位）。"""
    raw = str(members_text or "").strip()
    if not raw:
        return 0
    best = 0
    for token in re.findall(r"\d+(?:[.,]\d+)?\s*[KkMmBb万亿]?", raw):
        t = token.replace(" ", "").replace("\u00a0", "")
        mult = 1.0
        low = t.lower()
        if low.endswith("k"):
            mult, t = 1_000.0, t[:-1]
        elif low.endswith("m"):
            mult, t = 1_000_000.0, t[:-1]
        elif low.endswith("b"):
            mult, t = 1_000_000_000.0, t[:-1]
        elif t.endswith("万"):
            mult, t = 10_000.0, t[:-1]
        elif t.endswith("亿"):
            mult, t = 100_000_000.0, t[:-1]
        cleaned = t.replace(",", "")
        # 若形如 12.345 且无小数倍增符，按千分位处理
        if mult == 1.0 and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
            cleaned = cleaned.replace(".", "")
        try:
            val = int(float(cleaned) * mult)
            if val > best:
                best = val
        except ValueError:
            continue
    return best


def validate_final_comment_text(text: str) -> str:
    """校验评论最终文案，拒绝未展开的 `{a|b}` 变体或 JSON 数组。"""
    val = str(text or "").strip()
    if not val:
        return "评论文本不能为空"
    if _SPINTAX_RE.search(val):
        return "评论文本包含未展开的 {a|b} 变体，请传入最终文本"
    if val.startswith("[") and val.endswith("]"):
        try:
            if isinstance(json.loads(val), list):
                return "评论文本不能是 JSON 数组，请传入单条最终文案"
        except Exception:
            pass
    return ""
