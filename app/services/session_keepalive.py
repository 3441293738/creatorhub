"""全平台账号会话保活、Cookie/Token 续期合并与安全落库工具。

设计原则：
1. 严格域隔离：仅接收并合并目标平台第一方域名的 Cookie，绝不跨站/跨平台混入
   （例如海外小红书 Rednote 严禁接收 xiaohongshu.com Cookie）。
2. 只在确认登录态有效后持久化：防止把登出/风控重定向返回的访客 Cookie 覆盖进数据库，
   且核心鉴权 Cookie 缺失时拒绝落库。
3. 乐观并发控制（CAS）：落库前校验数据库当前快照是否与本次探测基线一致，
   避免后台保活覆写用户刚刚完成的手动重新登录。
4. 保留 localStorage / origins 关键环境数据（如 B 站 ac_time_value、小红书 b1、抖音 xmst）。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from app.db import get_session
from app.models import DouyinAccount

log = logging.getLogger("creatorhub.keepalive")

PLATFORM_DEFAULT_DOMAIN: dict[str, str] = {
    "douyin": ".douyin.com",
    "xhs": ".xiaohongshu.com",
    "rednote": ".rednote.com",
    "kuaishou": ".kuaishou.com",
    "shipinhao": ".weixin.qq.com",
    "tiktok": ".tiktok.com",
    "bilibili": ".bilibili.com",
    "instagram": ".instagram.com",
    "twitter": ".x.com",
}

PLATFORM_ALLOWED_ROOTS: dict[str, tuple[str, ...]] = {
    "douyin": ("douyin.com", "snssdk.com", "bytedance.com", "iesdouyin.com"),
    "xhs": ("xiaohongshu.com",),
    "rednote": ("rednote.com",),
    "kuaishou": ("kuaishou.com",),
    "shipinhao": ("weixin.qq.com", "qq.com"),
    "tiktok": ("tiktok.com",),
    "bilibili": ("bilibili.com",),
    "instagram": ("instagram.com", "instagr.am"),
    "twitter": ("x.com", "twitter.com"),
}

# 各平台核心登录凭据 Cookie（用于防止误删核心凭据、以及估算会话临期状态）
PLATFORM_CORE_COOKIES: dict[str, tuple[str, ...]] = {
    "douyin": ("sessionid", "sessionid_ss", "sid_tt", "uid_tt", "sid_guard", "passport_csrf_token"),
    "xhs": ("web_session", "a1"),
    "rednote": ("web_session", "a1"),
    "kuaishou": ("passToken", "userId", "kuaishou.server.web_st", "kuaishou.server.web_ph"),
    "shipinhao": ("_finder_auth", "sessionid", "wxuin", "finder_username"),
    "tiktok": ("sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt"),
    "bilibili": ("SESSDATA", "bili_jct", "DedeUserID"),
    "instagram": ("sessionid", "ds_user_id", "csrftoken"),
    "twitter": ("auth_token", "ct0", "twid"),
}

XHS_CREATOR_CORE_COOKIES: tuple[str, ...] = (
    "galaxy_creator_session_id",
    "access-token-creator.xiaohongshu.com",
    "customer-sso-sid",
    "a1",
)

REDNOTE_CREATOR_CORE_COOKIES: tuple[str, ...] = (
    "galaxy_creator_session_id",
    "access-token-creator.rednote.com",
    "customer-sso-sid",
    "customerClientId",
    "a1",
)

_COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
_DUAL_SYNC_CREATOR_PLATFORMS = {"kuaishou", "shipinhao", "bilibili", "instagram", "twitter"}


def _clean_domain(domain: Any) -> str:
    return str(domain or "").strip().lstrip(".").lower()


def domain_allowed_for_platform(platform: str, domain: Any) -> bool:
    """校验 Cookie/Origin 域名是否属于指定平台的第一方域。"""
    roots = PLATFORM_ALLOWED_ROOTS.get(str(platform or "").strip().lower())
    if not roots:
        return True
    clean = _clean_domain(domain)
    if not clean:
        return True
    return any(clean == root or clean.endswith("." + root) for root in roots)


def _cookie_identity(cookie: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(cookie.get("name") or "").strip(),
        _clean_domain(cookie.get("domain", "")),
        str(cookie.get("path") or "/").strip() or "/",
    )


def _parse_state_dict(raw: str | Mapping[str, Any] | None) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        data = deepcopy(dict(raw))
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            data = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            data = {}
    else:
        data = {}
    cookies = data.get("cookies")
    origins = data.get("origins")
    return {
        "cookies": [dict(c) for c in cookies if isinstance(c, Mapping)] if isinstance(cookies, list) else [],
        "origins": [deepcopy(o) for o in origins if isinstance(o, Mapping)] if isinstance(origins, list) else [],
    }


def _sanitize_cookie(cookie: Mapping[str, Any], *, default_domain: str = "") -> dict[str, Any] | None:
    name = str(cookie.get("name") or "").strip()
    if not name:
        return None
    out: dict[str, Any] = {
        "name": name,
        "value": str(cookie.get("value") if cookie.get("value") is not None else ""),
        "domain": str(cookie.get("domain") or default_domain).strip(),
        "path": str(cookie.get("path") or "/").strip() or "/",
    }
    if "expires" in cookie and cookie["expires"] is not None and not isinstance(cookie["expires"], bool):
        try:
            exp = float(cookie["expires"])
            if math.isfinite(exp):
                out["expires"] = exp
        except (TypeError, ValueError, OverflowError):
            pass
    if "httpOnly" in cookie:
        out["httpOnly"] = bool(cookie["httpOnly"])
    if "secure" in cookie:
        out["secure"] = bool(cookie["secure"])
    same_site = str(cookie.get("sameSite") or "").strip().title()
    if same_site in {"Strict", "Lax", "None"}:
        out["sameSite"] = same_site
    return out


def _is_expired(cookie: Mapping[str, Any], now: float) -> bool:
    exp = cookie.get("expires")
    if exp is None or isinstance(exp, bool):
        return False
    try:
        val = float(exp)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(val):
        return False
    return val > 0 and val <= now


def _protected_cookie_names(platform: str, scope: str = "read") -> set[str]:
    pf = str(platform or "").strip().lower()
    names = set(PLATFORM_CORE_COOKIES.get(pf, ()))
    if pf == "xhs":
        names.update(XHS_CREATOR_CORE_COOKIES)
    elif pf == "rednote":
        names.update(REDNOTE_CREATOR_CORE_COOKIES)
    return names


def has_required_auth_cookies(
    platform: str,
    state: str | Mapping[str, Any] | None,
    *,
    scope: str = "read",
) -> bool:
    """确认快照中仍包含平台的核心登录凭据，避免把访客态误覆盖进数据库。"""
    parsed = _parse_state_dict(state)
    pf = str(platform or "").strip().lower()
    now = time.time()
    active_map: dict[str, str] = {}
    for item in parsed["cookies"]:
        if not domain_allowed_for_platform(pf, item.get("domain", "")):
            continue
        if _is_expired(item, now):
            continue
        name = str(item.get("name") or "").strip()
        val = str(item.get("value") or "").strip()
        if name and val:
            active_map[name] = val

    if not active_map:
        return False
    if pf == "bilibili":
        return bool(active_map.get("SESSDATA") and active_map.get("bili_jct"))
    if pf == "twitter":
        return bool(active_map.get("auth_token"))
    if pf == "instagram":
        return bool(active_map.get("sessionid"))
    if pf == "xhs":
        if scope == "creator":
            return bool(active_map.get("a1")) and any(
                active_map.get(k) for k in XHS_CREATOR_CORE_COOKIES if k != "a1"
            )
        return bool(active_map.get("a1") and active_map.get("web_session"))
    if pf == "rednote":
        if scope == "creator":
            return bool(active_map.get("a1")) and any(
                active_map.get(k) for k in REDNOTE_CREATOR_CORE_COOKIES if k != "a1"
            )
        return bool(active_map.get("a1") and (
            active_map.get("web_session")
            or any(active_map.get(k) for k in REDNOTE_CREATOR_CORE_COOKIES if k != "a1")
        ))
    if pf == "shipinhao":
        return bool(
            active_map.get("_finder_auth")
            or active_map.get("sessionid")
            or active_map.get("wxuin")
        )
    if pf == "kuaishou":
        return bool(
            active_map.get("passToken")
            or active_map.get("userId")
            or active_map.get("kuaishou.server.web_st")
        )
    if pf == "tiktok":
        return bool(
            active_map.get("sessionid")
            or active_map.get("sessionid_ss")
            or active_map.get("sid_tt")
        )
    if pf == "douyin":
        return any(
            active_map.get(k)
            for k in ("sessionid", "sessionid_ss", "sid_tt", "uid_tt", "sid_guard", "passport_auth_status")
        )
    return True


def extract_set_cookie_lines(response: Any) -> list[str]:
    """兼容 curl_cffi / httpx / dict 响应头，提取所有 Set-Cookie 原始行。"""
    headers = getattr(response, "headers", None)
    if headers is None and isinstance(response, Mapping):
        headers = response
    if headers is None:
        return []

    lines: list[str] = []
    get_list = getattr(headers, "get_list", None)
    if callable(get_list):
        try:
            raw_list = get_list("set-cookie")
            if isinstance(raw_list, (list, tuple)):
                lines = [str(item).strip() for item in raw_list if item and str(item).strip()]
                if lines:
                    return lines
        except Exception:
            pass

    multi_items = getattr(headers, "multi_items", None)
    if callable(multi_items):
        try:
            for k, v in multi_items():
                if str(k).lower() == "set-cookie" and v:
                    lines.append(str(v).strip())
            if lines:
                return lines
        except Exception:
            pass

    raw_pairs = getattr(headers, "raw", None)
    if isinstance(raw_pairs, (list, tuple)):
        for pair in raw_pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            k, v = pair
            k_str = k.decode("latin-1", "ignore") if isinstance(k, (bytes, bytearray)) else str(k)
            if k_str.lower() == "set-cookie":
                v_str = v.decode("latin-1", "ignore") if isinstance(v, (bytes, bytearray)) else str(v)
                if v_str.strip():
                    lines.append(v_str.strip())
        if lines:
            return lines

    if isinstance(headers, Mapping):
        for k, v in headers.items():
            if str(k).lower() != "set-cookie" or not v:
                continue
            if isinstance(v, (list, tuple)):
                lines.extend(str(item).strip() for item in v if item and str(item).strip())
            else:
                lines.append(str(v).strip())
    return lines


def _parse_set_cookie_line(
    line: str,
    *,
    url: str = "",
    default_domain: str = "",
    now: float | None = None,
) -> list[tuple[dict[str, Any], bool]]:
    """按 RFC 6265 解析单条 Set-Cookie 响应头，兼容值中含逗号/冒号/等号等字符。

    返回 `[(cookie_dict, is_deleted)]`。
    """
    text = str(line or "").strip()
    if not text or "=" not in text:
        return []
    now_ts = time.time() if now is None else now
    parsed_url = urlsplit(url) if url else None
    req_host = (parsed_url.hostname or "").lower() if parsed_url else ""
    req_dir = (
        (parsed_url.path.rpartition("/")[0] or "/")
        if (parsed_url and parsed_url.path)
        else "/"
    )

    parts = text.split(";")
    name_part, sep, val_part = parts[0].partition("=")
    clean_name = name_part.strip()
    if not sep or not clean_name:
        return []
    clean_val = val_part.strip()
    if len(clean_val) >= 2 and clean_val[0] == '"' and clean_val[-1] == '"':
        clean_val = clean_val[1:-1]

    raw_domain = ""
    raw_path = ""
    max_age_raw = ""
    expires_raw = ""
    http_only = False
    secure = False
    same_site_raw = ""

    for attr in parts[1:]:
        seg = attr.strip()
        if not seg:
            continue
        if "=" in seg:
            ak, _, av = seg.partition("=")
            key_low = ak.strip().lower()
            val_str = av.strip()
            if key_low == "domain":
                raw_domain = val_str
            elif key_low == "path":
                raw_path = val_str
            elif key_low == "max-age":
                max_age_raw = val_str
            elif key_low == "expires":
                expires_raw = val_str
            elif key_low == "samesite":
                same_site_raw = val_str.title()
        else:
            flag_low = seg.lower()
            if flag_low == "httponly":
                http_only = True
            elif flag_low == "secure":
                secure = True

    if raw_domain:
        dom = raw_domain.lower()
        if not dom.startswith("."):
            dom = "." + dom
    elif req_host:
        dom = req_host
    else:
        dom = default_domain

    path = raw_path or req_dir or "/"
    expires_val: float = -1.0
    deleted = False

    if max_age_raw:
        try:
            max_age = int(float(max_age_raw))
            if max_age <= 0:
                deleted = True
                expires_val = 0.0
            else:
                expires_val = now_ts + max_age
        except (TypeError, ValueError, OverflowError):
            pass
    elif expires_raw:
        try:
            dt = parsedate_to_datetime(expires_raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
            if ts <= now_ts:
                deleted = True
            expires_val = ts
        except (TypeError, ValueError, OverflowError):
            pass

    ck: dict[str, Any] = {
        "name": clean_name,
        "value": clean_val,
        "domain": dom,
        "path": path,
        "expires": expires_val,
        "httpOnly": http_only,
        "secure": secure,
    }
    if same_site_raw in {"Strict", "Lax", "None"}:
        ck["sameSite"] = same_site_raw
    return [(ck, deleted)]


def _cookies_meaningfully_changed(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    expiry_threshold_seconds: float = 300.0,
) -> bool:
    before_map = {_cookie_identity(c): c for c in before}
    after_map = {_cookie_identity(c): c for c in after}
    if set(before_map) != set(after_map):
        return True
    for key, new_ck in after_map.items():
        old_ck = before_map[key]
        if str(old_ck.get("value") or "") != str(new_ck.get("value") or ""):
            return True
        old_exp = old_ck.get("expires")
        new_exp = new_ck.get("expires")
        try:
            old_num = float(old_exp) if old_exp is not None else -1.0
        except (TypeError, ValueError):
            old_num = -1.0
        try:
            new_num = float(new_exp) if new_exp is not None else -1.0
        except (TypeError, ValueError):
            new_num = -1.0
        if (old_num > 0) != (new_num > 0):
            return True
        if old_num > 0 and new_num > 0 and abs(new_num - old_num) >= expiry_threshold_seconds:
            return True
    return False


def merge_set_cookies_into_state(
    base_state: str | Mapping[str, Any] | None,
    set_cookie_lines: Iterable[str | tuple[str, str]] = (),
    *,
    platform: str,
    url: str = "",
    cookie_updates: Mapping[str, str] | None = None,
    scope: str = "read",
    default_ttl_seconds: int = 0,
    mirror_domains: tuple[str, ...] = (),
) -> tuple[dict[str, Any] | None, bool]:
    """把 HTTP 响应 `Set-Cookie` 与显式 Cookie 字典增量合并回账号 `storage_state`。

    - `set_cookie_lines` 支持 `list[str]` 或 `list[(request_url, set_cookie_line)]`。
    - 仅合并属于该平台第一方域的 Cookie。
    - 不会因为异常响应误清空受保护的核心登录凭据。
    """
    pf = str(platform or "").strip().lower()
    default_domain = PLATFORM_DEFAULT_DOMAIN.get(pf, "")
    state = _parse_state_dict(base_state)
    before_cookies = deepcopy(state["cookies"])
    protected = _protected_cookie_names(pf, scope=scope)
    now = time.time()

    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in state["cookies"]:
        sanitized = _sanitize_cookie(item, default_domain=default_domain)
        if sanitized is not None:
            by_key[_cookie_identity(sanitized)] = sanitized

    # 1) 合并原始 Set-Cookie 响应头（保留准确 domain / path / expires / flags）
    for entry in set_cookie_lines or ():
        if isinstance(entry, tuple) and len(entry) == 2:
            item_url, raw_line = str(entry[0] or url), str(entry[1] or "")
        else:
            item_url, raw_line = url, str(entry or "")
        for ck, deleted in _parse_set_cookie_line(
            raw_line, url=item_url, default_domain=default_domain, now=now
        ):
            if not domain_allowed_for_platform(pf, ck.get("domain", "")):
                continue
            key = _cookie_identity(ck)
            name = key[0]
            if deleted:
                if name in protected:
                    continue
                by_key.pop(key, None)
                continue
            if not ck.get("value") and name in protected:
                continue
            # 保留旧 cookie 已有的合理属性（例如响应未重复下发 sameSite）
            prev = by_key.get(key)
            if prev is not None and "sameSite" not in ck and "sameSite" in prev:
                ck["sameSite"] = prev["sameSite"]
            by_key[key] = ck

    # 2) 合并键值对形式的 Cookie 更新（例如 B站/Twitter/IG 客户端内存中刷新出的键值）
    if cookie_updates:
        target_domains = mirror_domains or ((default_domain,) if default_domain else ("",))
        for raw_name, raw_val in cookie_updates.items():
            name = str(raw_name or "").strip()
            value = str(raw_val or "").strip()
            if not name or not value:
                continue
            matched_keys = [
                k for k in list(by_key)
                if k[0] == name and domain_allowed_for_platform(pf, by_key[k].get("domain", ""))
            ]
            if matched_keys:
                for k in matched_keys:
                    existing = by_key[k]
                    if existing.get("value") != value:
                        existing["value"] = value
                        if default_ttl_seconds > 0:
                            existing["expires"] = now + default_ttl_seconds
                    elif default_ttl_seconds > 0:
                        cur_exp = existing.get("expires")
                        try:
                            cur_num = float(cur_exp) if cur_exp is not None else -1.0
                        except (TypeError, ValueError):
                            cur_num = -1.0
                        if cur_num > 0 and cur_num < now + default_ttl_seconds * 0.5:
                            existing["expires"] = now + default_ttl_seconds
            else:
                for dom in target_domains:
                    if not domain_allowed_for_platform(pf, dom):
                        continue
                    new_ck: dict[str, Any] = {
                        "name": name,
                        "value": value,
                        "domain": dom,
                        "path": "/",
                        "secure": True,
                        "httpOnly": name in {"SESSDATA", "auth_token", "sessionid", "sessionid_ss", "passToken"},
                    }
                    if default_ttl_seconds > 0:
                        new_ck["expires"] = now + default_ttl_seconds
                    by_key[_cookie_identity(new_ck)] = new_ck

        # Twitter/X 保持 .x.com 与 .twitter.com 双域一致
        if pf == "twitter" and mirror_domains:
            for raw_name, raw_val in cookie_updates.items():
                name = str(raw_name or "").strip()
                value = str(raw_val or "").strip()
                if not name or not value:
                    continue
                for dom in mirror_domains:
                    key = (name, _clean_domain(dom), "/")
                    if key not in by_key:
                        new_ck = {
                            "name": name,
                            "value": value,
                            "domain": dom,
                            "path": "/",
                            "secure": True,
                            "httpOnly": name in {"auth_token", "kdt", "att"},
                            "sameSite": "Lax" if name == "ct0" else "None",
                        }
                        if default_ttl_seconds > 0:
                            new_ck["expires"] = now + default_ttl_seconds
                        by_key[key] = new_ck

    merged_cookies = list(by_key.values())
    state["cookies"] = merged_cookies
    if not has_required_auth_cookies(pf, state, scope=scope):
        return None, False
    changed = _cookies_meaningfully_changed(before_cookies, merged_cookies)
    return state, changed


def merge_browser_state(
    base_state: str | Mapping[str, Any] | None,
    live_state: Mapping[str, Any] | None,
    *,
    platform: str,
    scope: str = "read",
) -> tuple[dict[str, Any] | None, bool]:
    """将浏览器上下文导出的 live storage_state 安全增量合并到数据库快照中。

    - 严格过滤非目标平台第一方域的 Cookie（不带入第三方广告/跟踪 Cookie）。
    - 保留数据库旧快照中尚未过期的其他子域同平台 Cookie 与 localStorage，避免单页访问丢数据。
    - 用浏览器 live 中同键非空 Cookie 覆盖旧值并刷新 expires。
    """
    if not isinstance(live_state, Mapping):
        return None, False
    pf = str(platform or "").strip().lower()
    default_domain = PLATFORM_DEFAULT_DOMAIN.get(pf, "")
    base = _parse_state_dict(base_state)
    live = _parse_state_dict(live_state)
    if not live["cookies"]:
        return None, False

    now = time.time()
    before_cookies = deepcopy(base["cookies"])
    before_origins = deepcopy(base["origins"])
    protected = _protected_cookie_names(pf, scope=scope)

    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in base["cookies"]:
        sanitized = _sanitize_cookie(item, default_domain=default_domain)
        if sanitized is None:
            continue
        if not domain_allowed_for_platform(pf, sanitized.get("domain", "")):
            continue
        if _is_expired(sanitized, now):
            continue
        by_key[_cookie_identity(sanitized)] = sanitized

    for item in live["cookies"]:
        sanitized = _sanitize_cookie(item, default_domain=default_domain)
        if sanitized is None:
            continue
        if not domain_allowed_for_platform(pf, sanitized.get("domain", "")):
            continue
        if _is_expired(sanitized, now):
            continue
        if not sanitized.get("value") and sanitized["name"] in protected:
            continue
        by_key[_cookie_identity(sanitized)] = sanitized

    merged_cookies = list(by_key.values())

    # 按 origin 合并 localStorage（保留旧键，用 live 新非空值覆盖）
    origins_map: dict[str, dict[str, str]] = {}
    for origin_entry in base["origins"]:
        orig = str(origin_entry.get("origin") or "").strip()
        if not orig:
            continue
        host = (urlsplit(orig).hostname or "").lower()
        if host and not domain_allowed_for_platform(pf, host):
            continue
        ls_map = origins_map.setdefault(orig, {})
        for kv in origin_entry.get("localStorage") or []:
            if isinstance(kv, Mapping) and kv.get("name"):
                ls_map[str(kv["name"])] = str(kv.get("value") or "")

    for origin_entry in live["origins"]:
        orig = str(origin_entry.get("origin") or "").strip()
        if not orig:
            continue
        host = (urlsplit(orig).hostname or "").lower()
        if host and not domain_allowed_for_platform(pf, host):
            continue
        ls_map = origins_map.setdefault(orig, {})
        for kv in origin_entry.get("localStorage") or []:
            if isinstance(kv, Mapping) and kv.get("name"):
                val = str(kv.get("value") or "")
                if val or str(kv["name"]) not in ls_map:
                    ls_map[str(kv["name"])] = val

    merged_origins = [
        {
            "origin": orig,
            "localStorage": [{"name": k, "value": v} for k, v in sorted(ls_map.items())],
        }
        for orig, ls_map in sorted(origins_map.items())
        if ls_map
    ]

    merged_state = {"cookies": merged_cookies, "origins": merged_origins}
    if not has_required_auth_cookies(pf, merged_state, scope=scope):
        return None, False

    cookies_changed = _cookies_meaningfully_changed(before_cookies, merged_cookies)
    origins_changed = _canonical_origins(before_origins) != _canonical_origins(merged_origins)
    return merged_state, (cookies_changed or origins_changed)


def _canonical_origins(origins: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for entry in origins or []:
        if not isinstance(entry, Mapping):
            continue
        orig = str(entry.get("origin") or "").strip()
        if not orig:
            continue
        ls: dict[str, str] = {}
        for kv in entry.get("localStorage") or []:
            if isinstance(kv, Mapping) and kv.get("name"):
                ls[str(kv["name"])] = str(kv.get("value") or "")
        if ls:
            out[orig] = ls
    return out


def extract_bili_refresh_token(storage_state: str | Mapping[str, Any] | None) -> str:
    """从 B 站 `storage_state.origins[].localStorage` 提取官方 `ac_time_value` 续期令牌。"""
    state = _parse_state_dict(storage_state)
    preferred_hosts = ("www.bilibili.com", "passport.bilibili.com", "member.bilibili.com")
    fallback = ""
    for entry in state["origins"]:
        origin = str(entry.get("origin") or "").strip()
        host = (urlsplit(origin).hostname or "").lower()
        if not domain_allowed_for_platform("bilibili", host):
            continue
        for kv in entry.get("localStorage") or []:
            if not isinstance(kv, Mapping):
                continue
            if str(kv.get("name") or "") != "ac_time_value":
                continue
            val = str(kv.get("value") or "").strip().strip('"')
            if not val:
                continue
            if host in preferred_hosts:
                return val
            if not fallback:
                fallback = val
    return fallback


def upsert_local_storage_value(
    state: dict[str, Any],
    origin: str,
    name: str,
    value: str,
    *,
    update_existing_hosts: tuple[str, ...] = (),
) -> bool:
    """向 `storage_state.origins` 写入或更新指定 `localStorage` 键值。"""
    clean_val = str(value or "").strip()
    if not name or not clean_val:
        return False
    origins = state.setdefault("origins", [])
    if not isinstance(origins, list):
        origins = []
        state["origins"] = origins

    changed = False
    found_target = False
    for entry in origins:
        if not isinstance(entry, dict):
            continue
        entry_origin = str(entry.get("origin") or "").strip()
        entry_host = (urlsplit(entry_origin).hostname or "").lower()
        should_touch = entry_origin == origin or (
            update_existing_hosts and any(
                entry_host == h or entry_host.endswith("." + h)
                for h in update_existing_hosts
            )
        )
        if not should_touch:
            continue
        if entry_origin == origin:
            found_target = True
        ls = entry.setdefault("localStorage", [])
        if not isinstance(ls, list):
            ls = []
            entry["localStorage"] = ls
        updated = False
        for kv in ls:
            if isinstance(kv, dict) and str(kv.get("name") or "") == name:
                if str(kv.get("value") or "") != clean_val:
                    kv["value"] = clean_val
                    changed = True
                updated = True
                break
        if not updated and entry_origin == origin:
            ls.append({"name": name, "value": clean_val})
            changed = True

    if not found_target:
        origins.append({
            "origin": origin,
            "localStorage": [{"name": name, "value": clean_val}],
        })
        changed = True
    return changed


def inspect_session_expiry(
    platform: str,
    storage_state: str | Mapping[str, Any] | None,
    creator_storage_state: str | Mapping[str, Any] | None = "",
) -> dict[str, Any]:
    """评估账号当前持久化登录态的有效期健康度，供接口展示与临期优先调度使用。"""
    pf = str(platform or "").strip().lower()
    raw = storage_state or creator_storage_state
    state = _parse_state_dict(raw)
    now = time.time()
    core_names = set(PLATFORM_CORE_COOKIES.get(pf, ()))
    if pf == "xhs" and not storage_state and creator_storage_state:
        core_names = set(XHS_CREATOR_CORE_COOKIES)
    elif pf == "rednote" and not storage_state and creator_storage_state:
        core_names = set(REDNOTE_CREATOR_CORE_COOKIES)

    found_core = 0
    finite_expiries: list[float] = []
    session_only_core = 0
    expired_core = 0

    for item in state["cookies"]:
        if not domain_allowed_for_platform(pf, item.get("domain", "")):
            continue
        name = str(item.get("name") or "").strip()
        val = str(item.get("value") or "").strip()
        if not name or not val or (core_names and name not in core_names):
            continue
        found_core += 1
        exp = item.get("expires")
        if exp is None or isinstance(exp, bool):
            session_only_core += 1
            continue
        try:
            exp_ts = float(exp)
        except (TypeError, ValueError, OverflowError):
            session_only_core += 1
            continue
        if not math.isfinite(exp_ts) or exp_ts <= 0:
            session_only_core += 1
        elif exp_ts <= now:
            expired_core += 1
        else:
            finite_expiries.append(exp_ts)

    earliest = min(finite_expiries) if finite_expiries else None
    expires_in_hours = round((earliest - now) / 3600.0, 1) if earliest is not None else None
    expiring_soon = bool(
        (expires_in_hours is not None and expires_in_hours <= 24.0)
        or (expired_core > 0 and not finite_expiries)
    )
    has_refresh_token = (
        bool(extract_bili_refresh_token(state))
        if pf == "bilibili"
        else False
    )
    return {
        "core_cookie_count": found_core,
        "session_only": bool(found_core > 0 and not finite_expiries and session_only_core > 0),
        "earliest_expiry_at": (
            datetime.fromtimestamp(earliest, tz=timezone.utc).replace(tzinfo=None).isoformat()
            if earliest is not None else None
        ),
        "expires_in_hours": expires_in_hours,
        "expiring_soon": expiring_soon,
        "has_refresh_token": has_refresh_token,
    }


def persist_refreshed_session(
    account_id: int,
    platform: str,
    new_state: dict[str, Any] | str,
    *,
    expected_state: str = "",
    expected_creator_state: str = "",
    scope: str = "read",
    sync_creator: bool | None = None,
    sync_cookie_str: bool = False,
    keepalive_note: str = "",
) -> bool:
    """将保活/续期后的最新 `storage_state` 以乐观锁方式安全落库。"""
    if not account_id:
        return False
    pf = str(platform or "").strip().lower()
    state_dict = _parse_state_dict(new_state)
    if not state_dict["cookies"]:
        return False
    if not has_required_auth_cookies(pf, state_dict, scope=scope):
        log.debug("账号 %s(%s) 新快照缺少核心登录凭据，已拒绝覆盖落库", account_id, pf)
        return False

    state_json = json.dumps(state_dict, ensure_ascii=False)
    now = datetime.utcnow()
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account is None or str(account.platform or "").lower() != pf:
            return False

        if scope == "creator" and pf in {"xhs", "rednote"}:
            if expected_creator_state and account.creator_storage_state != expected_creator_state:
                log.debug("账号 %s(%s) 创作者登录态已被人工更新，跳过后台覆盖", account_id, pf)
                return False
            account.creator_storage_state = state_json
        else:
            if expected_state and account.storage_state != expected_state:
                log.debug("账号 %s(%s) 主站登录态已被人工更新，跳过后台覆盖", account_id, pf)
                return False
            old_storage = account.storage_state
            account.storage_state = state_json
            should_sync_creator = (
                bool(sync_creator)
                if sync_creator is not None
                else (
                    pf in _DUAL_SYNC_CREATOR_PLATFORMS
                    or (
                        pf == "rednote"
                        and (not account.creator_storage_state or account.creator_storage_state == old_storage)
                    )
                )
            )
            if should_sync_creator:
                account.creator_storage_state = state_json

        if scope == "read" and (
            sync_cookie_str or (account.cookie and pf in {"twitter", "bilibili", "instagram", "douyin", "xhs", "rednote"})
        ):
            try:
                if pf == "twitter":
                    from app.platforms.twitter.protocol import cookie_str_from_state as tw_cookie_str
                    account.cookie = tw_cookie_str(state_dict)
                elif pf == "bilibili":
                    from app.platforms.bilibili.client import cookie_str_from_state as bili_cookie_str
                    account.cookie = bili_cookie_str(state_dict)
                elif pf == "instagram":
                    from app.platforms.instagram.extract import cookie_str_from_state as ig_cookie_str
                    account.cookie = ig_cookie_str(state_dict)
                elif pf == "douyin":
                    from app.platforms.douyin.client import cookie_from_state as dy_cookie_str
                    account.cookie = dy_cookie_str(state_json)
                elif pf == "xhs":
                    from app.platforms.xhs.cookies import cookie_str_from_state as xhs_cookie_str
                    account.cookie = xhs_cookie_str(state_json)
                elif pf == "rednote":
                    from app.platforms.rednote.cookies import cookie_str_from_state as rn_cookie_str
                    account.cookie = rn_cookie_str(state_json)
            except Exception:
                pass

        account.last_keepalive_at = now
        account.last_cookie_refresh_at = now
        if keepalive_note:
            account.keepalive_note = keepalive_note[:200]
        session.add(account)
        session.commit()
    return True
