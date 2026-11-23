"""X (Twitter) 协议层原语：Web Session Cookie 提取、Bearer/CSRF 请求头、Syndication 令牌与离线 TOTP。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import struct
import time
from typing import Any, Dict, Mapping, Tuple

# X Web App 公开静态客户端 Bearer Token（与浏览器登录会话 auth_token + ct0 配合使用）
TWITTER_WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
TWITTER_WEB_BEARER_DECODED = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

SYNDICATION_TWEET_URL = "https://cdn.syndication.twimg.com/tweet-result"
OEMBED_TWEET_URL = "https://publish.twitter.com/oembed"
REST_VERIFY_CREDENTIALS_URL = "https://api.x.com/1.1/account/verify_credentials.json"
REST_SETTINGS_URL = "https://x.com/i/api/1.1/account/settings.json"
REST_USERS_SHOW_URL = "https://x.com/i/api/1.1/users/show.json"
REST_USERS_SEARCH_URL = "https://x.com/i/api/1.1/users/search.json"
REST_DM_INBOX_URL = "https://x.com/i/api/1.1/dm/inbox_initial_state.json"
REST_DM_CONVERSATION_TMPL = "https://x.com/i/api/1.1/dm/conversation/{conv_id}.json"

DEFAULT_TWITTER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _is_twitter_cookie_domain(domain: str) -> bool:
    d = (domain or "").strip().lower().lstrip(".")
    if not d:
        return True
    return d in {"x.com", "twitter.com"} or d.endswith(".x.com") or d.endswith(".twitter.com")


_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_HEX_40_TOKEN_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{40})(?![0-9a-fA-F])")
_LABELED_CT0_RE = re.compile(r"\bct0\s*[:=]\s*([0-9a-fA-F]{32,160})\b", re.IGNORECASE)
_LABELED_USER_RE = re.compile(
    r"(?:账号|用户名|username|screen_name|user)\s*[:：=\t ]*(?:\r?\n\s*)?@?([A-Za-z0-9_]{1,15})\b",
    re.IGNORECASE,
)
_RESERVED_CREDENTIAL_WORDS = {
    "cookie",
    "cookies",
    "token",
    "auth_token",
    "ct0",
    "twid",
    "http",
    "https",
    "password",
    "pass",
    "email",
    "mail",
}


def _merge_cookie_items(items: list[Any]) -> Dict[str, str]:
    legacy_vals: Dict[str, str] = {}
    x_vals: Dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        domain = str(item.get("domain") or "").strip().lower()
        if not name or not value or not _is_twitter_cookie_domain(domain):
            continue
        if "x.com" in domain:
            x_vals[name] = value
        else:
            legacy_vals[name] = value
    merged = dict(legacy_vals)
    merged.update(x_vals)
    return merged


def parse_twitter_credential_input(raw: str) -> Tuple[Dict[str, str], str]:
    """兼容解析 X/Twitter 多种登录凭证输入，返回 `(cookies_dict, hint_screen_name)`。

    支持格式：
    1. 40 位十六进制裸 `Token`（即 `auth_token` 会话令牌）
    2. 多字段组合文本（`账号----密码----Token`、`user:pass:token` 或多行「账号 / 密码 / Token」）
    3. 浏览器完整 `Cookie` 请求头（`auth_token=...; ct0=...; twid=...`）
    4. JSON Cookie 数组 `[{"name":"auth_token","value":"..."}]`
    5. Patchright / Playwright `storage_state` JSON `{"cookies": [...]}`
    """
    text = str(raw or "").strip()
    if not text:
        return {}, ""

    # 1) JSON 对象或 JSON Cookie 数组
    if text.startswith(("{", "[")):
        try:
            parsed_json = json.loads(text)
        except Exception:
            parsed_json = None
        if isinstance(parsed_json, list):
            return _merge_cookie_items(parsed_json), ""
        if isinstance(parsed_json, Mapping):
            if isinstance(parsed_json.get("cookies"), list):
                return _merge_cookie_items(parsed_json["cookies"]), ""
            flat = {
                str(k).strip(): str(v).strip()
                for k, v in parsed_json.items()
                if isinstance(v, (str, int)) and str(k).strip() and str(v).strip()
            }
            if flat:
                return flat, ""

    # 2) 标准 Cookie 键值对（兼容前缀 `Cookie:`）
    body = re.sub(r"^\s*cookie\s*:\s*", "", text, flags=re.IGNORECASE)
    out: Dict[str, str] = {}
    if "=" in body and "\n" not in body and "----" not in body:
        for part in body.split(";"):
            k, sep, v = part.strip().partition("=")
            k_clean, v_clean = k.strip(), v.strip().strip('"')
            if sep and _COOKIE_NAME_RE.fullmatch(k_clean) and v_clean:
                out[k_clean] = v_clean

    # 3) 若未从标准 Cookie 解析出 auth_token，则按多字段文本 / 裸 40 位 hex Token 提取
    if not out.get("auth_token"):
        m_tok = _HEX_40_TOKEN_RE.search(text)
        if m_tok:
            out["auth_token"] = m_tok.group(1).lower()
    if not out.get("ct0"):
        m_ct0 = _LABELED_CT0_RE.search(text)
        if m_ct0:
            out["ct0"] = m_ct0.group(1)

    # 4) 尝试从多字段文本提取辅助 @screen_name（用于首次资料反查或默认备注）
    hint_user = ""
    m_user = _LABELED_USER_RE.search(text)
    if m_user:
        cand = m_user.group(1).strip()
        if cand.lower() not in _RESERVED_CREDENTIAL_WORDS and not _HEX_40_TOKEN_RE.fullmatch(cand):
            hint_user = cand
    if not hint_user and any(sep in text for sep in ("----", "|", "\t", ":")):
        first_line = text.splitlines()[0].strip()
        first_seg = re.split(r"----|\||\t|:", first_line)[0].strip().lstrip("@")
        if (
            re.fullmatch(r"[A-Za-z0-9_]{1,15}", first_seg)
            and first_seg.lower() not in _RESERVED_CREDENTIAL_WORDS
        ):
            hint_user = first_seg

    return out, hint_user


def build_twitter_storage_state(cookies: Mapping[str, str]) -> str:
    """将 X Cookie 字典编码为同时覆盖 `.x.com` 与 `.twitter.com` 的持久化 `storage_state` JSON。"""
    expires = int(time.time()) + 365 * 86400
    items: list[Dict[str, Any]] = []
    for raw_k, raw_v in (cookies or {}).items():
        name = str(raw_k or "").strip()
        value = str(raw_v or "").strip()
        if not name or not value:
            continue
        is_http_only = name in {"auth_token", "kdt", "att"}
        same_site = "Lax" if name == "ct0" else "None"
        for dom in (".x.com", ".twitter.com"):
            items.append(
                {
                    "name": name,
                    "value": value,
                    "domain": dom,
                    "path": "/",
                    "expires": expires,
                    "httpOnly": is_http_only,
                    "secure": True,
                    "sameSite": same_site,
                }
            )
    return json.dumps({"cookies": items, "origins": []}, ensure_ascii=False)


def cookie_map_from_state(storage_state: str | Mapping[str, Any] | None) -> Dict[str, str]:
    """从 Patchright `storage_state` JSON、`auth_token` 令牌或原始 Cookie 字符串提取 X 会话 Cookie 字典。

    当 `.twitter.com` 与 `.x.com` 同时存在同名键时，优先采用 `.x.com` 的非空值。
    """
    if not storage_state:
        return {}

    if isinstance(storage_state, str):
        cmap, _ = parse_twitter_credential_input(storage_state)
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


def has_twitter_login_cookies(storage_state: str | Mapping[str, Any] | None) -> bool:
    """校验是否具备 X 已登录会话的核心 `auth_token` Cookie。"""
    cmap = cookie_map_from_state(storage_state)
    return bool(cmap.get("auth_token"))


def extract_twid_uid(cookies: Mapping[str, str]) -> str:
    """从 `twid` Cookie（形如 `u%3D44196397` 或 `"u=44196397"`）提取当前账号数字 `rest_id`。"""
    raw = str(cookies.get("twid") or "").strip().strip('"')
    if not raw:
        return ""
    m = re.search(r"u(?:=|%3D)(\d{4,25})", raw, re.IGNORECASE)
    return m.group(1) if m else ""


def build_twitter_web_headers(
    cookies: Mapping[str, str],
    *,
    user_agent: str = "",
    referer: str = "https://x.com/",
) -> Dict[str, str]:
    """构造 X Web Session API 请求头（自动注入 `Bearer`、`x-csrf-token` 与一致的 Client Hints）。"""
    ua = (user_agent or DEFAULT_TWITTER_UA).strip()
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
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f"Bearer {TWITTER_WEB_BEARER_DECODED}",
        "Origin": "https://x.com",
        "Referer": referer or "https://x.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "sec-ch-ua": brands,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
    }
    ct0 = str(cookies.get("ct0") or "").strip()
    if ct0:
        headers["x-csrf-token"] = ct0
    if cookies.get("auth_token"):
        headers["x-twitter-auth-type"] = "OAuth2Session"
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v)
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def syndication_token(tweet_id: str) -> str:
    """生成 `cdn.syndication.twimg.com/tweet-result` 所需的公开校验 token。"""
    digits = "".join(ch for ch in str(tweet_id or "") if ch.isdigit())
    if not digits:
        return "0"
    val = (float(digits) / 1e15) * math.pi
    # 转换为 36 进制小数字符串并剔除 `0.` 与 `.`
    int_part = int(val)
    frac = val - int_part
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    if int_part > 0:
        n = int_part
        buf = []
        while n > 0:
            n, rem = divmod(n, 36)
            buf.append(alphabet[rem])
        out.extend(reversed(buf))
    for _ in range(12):
        frac *= 36.0
        digit = int(frac)
        out.append(alphabet[max(0, min(35, digit))])
        frac -= digit
        if frac <= 1e-12:
            break
    token = "".join(out).lstrip("0")
    return token or "0"


def compute_totp(
    secret_key: str,
    *,
    timestamp: int | None = None,
    digits: int = 6,
    period: int = 30,
) -> str:
    """基于标准库实现的 RFC 6238 动态口令（TOTP）生成器。"""
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


def format_retweet_result_str(
    tweet_id: str,
    tweet_url: str,
    target_account: str = "",
    repin_count: int = 0,
) -> str:
    """按 `tweetId###url###account###count` 紧凑结构序列化转推记录字符串。"""
    return f"{tweet_id}###{tweet_url}###{target_account}###{int(repin_count)}"


def parse_retweet_result_str(result_str: str) -> Dict[str, Any]:
    """解析 `tweetId###url###account###count` 转推记录字符串。"""
    parts = str(result_str or "").split("###")
    if len(parts) < 4:
        return {
            "tweet_id": parts[0] if parts else "",
            "tweet_url": parts[1] if len(parts) > 1 else "",
            "target_account": parts[2] if len(parts) > 2 else "",
            "repin_count": 0,
        }
    try:
        cnt = int(parts[3])
    except ValueError:
        cnt = 0
    return {
        "tweet_id": parts[0],
        "tweet_url": parts[1],
        "target_account": parts[2],
        "repin_count": cnt,
    }
