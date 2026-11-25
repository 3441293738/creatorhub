"""YouTube 协议层原语：Google/YouTube Cookie 解析、SAPISIDHASH 签名、TOTP、数值归一化与参数契约。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
import time
from typing import Any, Dict, List, Mapping, Tuple

DEFAULT_YOUTUBE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Google / YouTube 核心已登录 Cookie（任一强会话组合即可视为已认证）
YOUTUBE_LOGIN_COOKIES = (
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "LOGIN_INFO",
)

_STRONG_AUTH_KEYS = frozenset({
    "SAPISID",
    "__Secure-3PAPISID",
    "__Secure-1PAPISID",
    "__Secure-3PSID",
    "__Secure-1PSID",
    "SID",
})

_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SPINTAX_RE = re.compile(r"\{[^{}]*\|[^{}]*\}", re.DOTALL)

# 仅属于 accounts.google.com 或响应头属性的键，绝不能混入 .youtube.com Cookie Jar（否则会触发 accounts.google.com/CookieMismatch）
_FORBIDDEN_YOUTUBE_COOKIES = frozenset({
    "LSID",
    "__Host-1PLSID",
    "__Host-3PLSID",
    "__Host-GAPS",
    "__Host-GAPSTS",
    "ACCOUNT_CHOOSER",
    "SMSV",
    "OTZ",
    "CONSISTENCY",
    "priority",
    "Priority",
    "path",
    "Path",
    "domain",
    "Domain",
    "expires",
    "Expires",
    "max-age",
    "Max-Age",
    "samesite",
    "SameSite",
    "secure",
    "Secure",
    "httponly",
    "HttpOnly",
})

_GOOGLE_SHARED_AUTH_KEYS = frozenset({
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
    "__Secure-1PSIDCC",
    "__Secure-3PSIDCC",
    "SIDCC",
})


def _is_forbidden_yt_cookie_name(name: str) -> bool:
    n = str(name or "").strip()
    if not n:
        return True
    if n in _FORBIDDEN_YOUTUBE_COOKIES or n.lower() in {"priority", "path", "domain", "expires", "max-age", "samesite", "secure", "httponly"}:
        return True
    if n.startswith("__Host-"):
        return True
    return False


def _is_youtube_cookie_domain(domain: str) -> bool:
    d = (domain or "").strip().lower().lstrip(".")
    if not d:
        return True
    return d in {"youtube.com", "youtu.be"} or d.endswith(".youtube.com")


def _is_google_root_cookie_domain(domain: str) -> bool:
    d = (domain or "").strip().lower().lstrip(".")
    return d == "google.com"


def _merge_cookie_items(items: List[Any]) -> Dict[str, str]:
    secondary: Dict[str, str] = {}
    primary: Dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        domain = str(item.get("domain") or "").strip().lower()
        if not name or not value or _is_forbidden_yt_cookie_name(name):
            continue
        if _is_youtube_cookie_domain(domain):
            primary[name] = value
        elif _is_google_root_cookie_domain(domain) and name in _GOOGLE_SHARED_AUTH_KEYS:
            # 仅兜底吸收 .google.com 共享核心鉴权键，绝不吸收 accounts.google.com 专属主机 Cookie
            secondary[name] = value
    merged = dict(secondary)
    merged.update(primary)
    return merged


def parse_youtube_credential_input(raw: str) -> Tuple[Dict[str, str], str]:
    """兼容解析多种 YouTube / Google 登录凭证输入，返回 `(cookies_dict, hint_label)`。

    支持格式：
    1. 浏览器完整 `Cookie` 字符串（`SID=...; SAPISID=...; __Secure-3PSID=...; LOGIN_INFO=...`）
    2. JSON Cookie 数组 `[{"name":"SAPISID","value":"..."}, ...]`（EditThisCookie / Cookie-Editor）
    3. Patchright / Playwright `storage_state` JSON `{"cookies": [...]}`
    4. 扁平 JSON 对象 `{"SAPISID": "...", "__Secure-3PSID": "..."}`
    5. 多字段账号文本（如 `email----pass----cookie` 或 `email|pass|2fa|SAPISID=...;__Secure-3PSID=...`）
    """
    text = str(raw or "").strip()
    if not text:
        return {}, ""

    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            cmap = _merge_cookie_items(parsed)
            return cmap, ""
        if isinstance(parsed, Mapping):
            if isinstance(parsed.get("cookies"), list):
                cmap = _merge_cookie_items(parsed["cookies"])
                return cmap, ""
            flat = {
                str(k).strip(): str(v).strip()
                for k, v in parsed.items()
                if isinstance(v, (str, int)) and str(k).strip() and str(v).strip()
            }
            if flat:
                return flat, ""

    body = re.sub(r"^\s*cookie\s*:\s*", "", text, flags=re.IGNORECASE)
    out: Dict[str, str] = {}
    hint = ""

    cookie_seg = body
    if "----" in body or "|" in body:
        parts = re.split(r"----|\|", body)
        for idx, seg in enumerate(parts):
            s = seg.strip()
            if any(f"{k}=" in s for k in ("SAPISID", "__Secure-3PSID", "LOGIN_INFO", "SID")):
                cookie_seg = s
                if idx > 0 and parts[0].strip() and "=" not in parts[0]:
                    hint = parts[0].strip()[:48]
                break

    if "=" in cookie_seg:
        for part in re.split(r";|\r?\n", cookie_seg):
            k, sep, v = part.strip().partition("=")
            k_clean = k.strip()
            v_clean = v.strip().strip('"')
            if sep and _COOKIE_NAME_RE.fullmatch(k_clean) and v_clean and not _is_forbidden_yt_cookie_name(k_clean):
                out[k_clean] = v_clean

    return out, hint


def build_youtube_storage_state(cookies: Mapping[str, str]) -> str:
    """将 YouTube/Google Cookie 字典编码为持久化 `storage_state` JSON（覆盖 `.youtube.com` 与 `.google.com`）。"""
    expires = int(time.time()) + 365 * 86400
    items: List[Dict[str, Any]] = []
    http_only_keys = {
        "HSID", "SSID", "__Secure-1PSID", "__Secure-3PSID",
        "__Secure-1PSIDTS", "__Secure-3PSIDTS", "LOGIN_INFO", "VISITOR_INFO1_LIVE",
    }
    google_shared_keys = {
        "SID", "HSID", "SSID", "APISID", "SAPISID",
        "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
        "__Secure-1PSIDTS", "__Secure-3PSIDTS", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
        "SIDCC", "NID",
    }
    for raw_k, raw_v in (cookies or {}).items():
        name = str(raw_k or "").strip()
        value = str(raw_v or "").strip()
        if not name or not value or _is_forbidden_yt_cookie_name(name):
            continue
        is_secure = name.startswith(("__Secure-", "__Host-")) or name in {"SSID", "SAPISID", "LOGIN_INFO"}
        same_site = "None" if (name.startswith("__Secure-3P") or name.startswith("VISITOR_")) else "Lax"
        items.append({
            "name": name,
            "value": value,
            "domain": ".youtube.com",
            "path": "/",
            "expires": expires,
            "httpOnly": name in http_only_keys,
            "secure": is_secure or True,
            "sameSite": same_site,
        })
        if name in google_shared_keys:
            items.append({
                "name": name,
                "value": value,
                "domain": ".google.com",
                "path": "/",
                "expires": expires,
                "httpOnly": name in http_only_keys,
                "secure": True,
                "sameSite": same_site,
            })
    return json.dumps({"cookies": items, "origins": []}, ensure_ascii=False)


def cookie_map_from_state(storage_state: str | Mapping[str, Any] | None) -> Dict[str, str]:
    """从 Patchright `storage_state` JSON 或 Cookie 文本提取 YouTube 会话 Cookie 字典。"""
    if not storage_state:
        return {}
    if isinstance(storage_state, str):
        cmap, _ = parse_youtube_credential_input(storage_state)
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


def has_youtube_login_cookies(storage_state: str | Mapping[str, Any] | None) -> bool:
    """校验是否具备 YouTube/Google 核心登录态 Cookie。"""
    cmap = cookie_map_from_state(storage_state)
    if not cmap:
        return False
    present_strong = [k for k in _STRONG_AUTH_KEYS if str(cmap.get(k) or "").strip()]
    if len(present_strong) >= 2:
        return True
    if str(cmap.get("LOGIN_INFO") or "").strip() and len(present_strong) >= 1:
        return True
    if str(cmap.get("SAPISID") or "").strip() or str(cmap.get("__Secure-3PAPISID") or "").strip():
        return bool(str(cmap.get("SID") or cmap.get("__Secure-3PSID") or cmap.get("__Secure-1PSID") or "").strip())
    return False


def compute_sapisidhash(
    cookies: Mapping[str, str],
    *,
    origin: str = "https://www.youtube.com",
    timestamp: int | None = None,
) -> str:
    """计算标准 YouTube Web `SAPISIDHASH` Authorization 头（用于已登录 Innertube POST）。"""
    sapisid = str(
        (cookies or {}).get("SAPISID")
        or (cookies or {}).get("__Secure-3PAPISID")
        or (cookies or {}).get("__Secure-1PAPISID")
        or ""
    ).strip()
    if not sapisid:
        return ""
    ts = int(time.time() if timestamp is None else timestamp)
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode("utf-8")).hexdigest()
    parts = [f"SAPISIDHASH {ts}_{digest}"]
    if (cookies or {}).get("__Secure-1PAPISID"):
        d1 = hashlib.sha1(f"{ts} {cookies['__Secure-1PAPISID']} {origin}".encode("utf-8")).hexdigest()
        parts.append(f"SAPISID1PHASH {ts}_{d1}")
    if (cookies or {}).get("__Secure-3PAPISID"):
        d3 = hashlib.sha1(f"{ts} {cookies['__Secure-3PAPISID']} {origin}".encode("utf-8")).hexdigest()
        parts.append(f"SAPISID3PHASH {ts}_{d3}")
    return " ".join(parts)


def build_youtube_web_headers(
    cookies: Mapping[str, str],
    *,
    user_agent: str = "",
    referer: str = "https://www.youtube.com/",
    accept_language: str = "en-US,en;q=0.9",
    include_cookie: bool = True,
) -> Dict[str, str]:
    """构造与账号浏览器画像一致的 YouTube Web 文档请求头。"""
    ua = (user_agent or DEFAULT_YOUTUBE_UA).strip()
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
        "Referer": referer or "https://www.youtube.com/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": brands,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
    }
    if include_cookie:
        # 默认携带 SOCS=CAI 避免欧洲/部分代理节点落入 GDPR consent 拦截页
        merged = dict(cookies or {})
        merged.setdefault("SOCS", "CAI")
        cookie_header = "; ".join(f"{k}={v}" for k, v in merged.items() if k and v)
        if cookie_header:
            headers["Cookie"] = cookie_header
    return headers


def build_innertube_context(
    *,
    client_version: str = "2.20250115.01.00",
    hl: str = "en",
    gl: str = "US",
    visitor_data: str = "",
) -> Dict[str, Any]:
    client_obj: Dict[str, Any] = {
        "hl": hl or "en",
        "gl": gl or "US",
        "clientName": "WEB",
        "clientVersion": client_version or "2.20250115.01.00",
        "originalUrl": "https://www.youtube.com/",
        "platform": "DESKTOP",
    }
    if visitor_data:
        client_obj["visitorData"] = visitor_data
    return {"client": client_obj}


def compute_totp(
    secret_key: str,
    *,
    timestamp: int | None = None,
    digits: int = 6,
    period: int = 30,
) -> str:
    """标准 RFC 6238 TOTP 生成器。"""
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


def parse_youtube_count(text: Any) -> int:
    """解析 YouTube 播放量/订阅数/点赞数文本（支持英文 K/M/B 与中文 万/亿、韩日文等数字格式）。"""
    if text is None or isinstance(text, bool):
        return 0
    if isinstance(text, (int, float)):
        return max(0, int(text))
    raw = str(text or "").strip()
    if not raw:
        return 0
    best = 0
    for token in re.findall(r"\d+(?:[.,]\d+)?\s*[KkMmBb万億亿千]?", raw):
        t = token.replace(" ", "").replace("\u00a0", "")
        mult = 1.0
        low = t.lower()
        if low.endswith("k"):
            mult, t = 1_000.0, t[:-1]
        elif low.endswith("m"):
            mult, t = 1_000_000.0, t[:-1]
        elif low.endswith("b"):
            mult, t = 1_000_000_000.0, t[:-1]
        elif t.endswith("千"):
            mult, t = 1_000.0, t[:-1]
        elif t.endswith("万"):
            mult, t = 10_000.0, t[:-1]
        elif t.endswith(("亿", "億")):
            mult, t = 100_000_000.0, t[:-1]
        cleaned = t.replace(",", "")
        if mult == 1.0 and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
            cleaned = cleaned.replace(".", "")
        try:
            val = int(float(cleaned) * mult)
            if val > best:
                best = val
        except ValueError:
            continue
    return best


def parse_duration_seconds(text: Any) -> int:
    """将 `MM:SS`、`HH:MM:SS`、`PT12M34S` 或毫秒/秒数字归一为整型秒数。"""
    if text is None or isinstance(text, bool):
        return 0
    if isinstance(text, (int, float)):
        n = int(text)
        return n // 1000 if n > 100_000 else max(0, n)
    s = str(text or "").strip()
    if not s:
        return 0
    if s.isdigit():
        n = int(s)
        return n // 1000 if n > 100_000 else max(0, n)
    if ":" in s:
        parts = [p.strip() for p in s.split(":")]
        if all(p.isdigit() for p in parts):
            nums = [int(p) for p in parts]
            if len(nums) == 2:
                return nums[0] * 60 + nums[1]
            if len(nums) == 3:
                return nums[0] * 3600 + nums[1] * 60 + nums[2]
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", s, re.IGNORECASE)
    if m:
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        sec = int(m.group(3) or 0)
        return h * 3600 + mi * 60 + sec
    return 0


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


def normalize_yt_publish_options(raw_opts: Mapping[str, Any] | str | None) -> Dict[str, Any]:
    """统一归一 YouTube Studio 发布扩展参数。"""
    if isinstance(raw_opts, str):
        try:
            data = json.loads(raw_opts) if raw_opts.strip() else {}
        except Exception:
            data = {}
    elif isinstance(raw_opts, Mapping):
        data = dict(raw_opts)
    else:
        data = {}

    post_kind = str(data.get("post_kind") or "video").strip().lower()
    if post_kind not in {"video", "shorts"}:
        post_kind = "video"

    visibility = str(data.get("visibility") or "").strip().lower()
    if bool(data.get("publish_as_private")):
        visibility = "private"
    if visibility not in {"public", "unlisted", "private"}:
        visibility = "public"

    def _split_list(val: Any) -> List[str]:
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        if isinstance(val, str):
            return [s.strip() for s in re.split(r"[,，\n]+", val) if s.strip()]
        return []

    return {
        "post_kind": post_kind,
        "channel_name": str(data.get("channel_name") or "").strip(),
        "visibility": visibility,
        "made_for_kids": bool(data.get("made_for_kids", False)),
        "tags": _split_list(data.get("tags")),
        "playlists": _split_list(data.get("playlists")),
        "thumbnail_path": str(data.get("thumbnail_path") or "").strip(),
        "category": str(data.get("category") or "").strip(),
        "language": str(data.get("language") or "").strip(),
        "paid_promotion": bool(data.get("paid_promotion", False)),
        "allow_embedding": bool(data.get("allow_embedding", True)),
        "notify_subscribers": bool(data.get("notify_subscribers", True)),
        "premiere": bool(data.get("premiere", False)),
        "first_comment": str(data.get("first_comment") or data.get("comment_text") or "").strip(),
    }
