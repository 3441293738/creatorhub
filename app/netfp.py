"""统一的 TLS/HTTP2 指纹复刻配置。

所有"裸 HTTP 直连平台接口"的地方(签名直连 API、扫码登录轮询、msToken 申请、
短链跳转、创作平台发布)都应改用 curl_cffi 的 impersonate,让请求的 JA3/JA4 与
HTTP2 指纹复刻真实 Chrome —— 纯 httpx 的 TLS 指纹与浏览器不同,容易被风控按
"非浏览器客户端"识别。

impersonate 目标按调用方传入的 UA 里的 Chrome 大版本自动挑最接近的,UA 升级后无需改这里。

注:浏览器拦截路径(Playwright 真实 Chrome)本身就是真指纹,不需要、也不应使用本模块。
"""
from __future__ import annotations

import re
from typing import Optional

# curl_cffi 0.15 支持的 Chrome impersonate 大版本(随库升级可补)。
_IMPERSONATE_CHROME = (100, 101, 104, 107, 110, 116, 119, 120, 123, 124,
                       131, 136, 142, 145, 146)

DEFAULT_IMPERSONATE = "chrome131"


def impersonate_for_ua(user_agent: str) -> str:
    """按 UA 里的 Chrome 大版本挑最接近的 impersonate 目标(对齐 TLS 指纹与 UA)。"""
    m = re.search(r"Chrome/(\d+)", user_agent or "")
    if not m:
        return DEFAULT_IMPERSONATE
    major = int(m.group(1))
    best = min(_IMPERSONATE_CHROME, key=lambda v: abs(v - major))
    return f"chrome{best}"


async def probe_ip_region(proxy: str = "", timeout: float = 8.0) -> Optional[dict]:
    """尽力探测出口 IP 的国家/地区 + 经纬度(经指定代理)。返回
    {'ip','country'(大写 ISO2),'lat','lon','city'} 或 None。country 用于「代理地区是否与
    账号时区一致」的告警;lat/lon 用于把该账号的 geolocation 伪造坐标对齐到真实出口地。
    任何失败都静默 None。"""
    from curl_cffi.requests import AsyncSession
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        async with AsyncSession() as cli:
            r = await cli.get("https://ipinfo.io/json", timeout=timeout,
                              impersonate=DEFAULT_IMPERSONATE, proxies=proxies)
            if r.status_code != 200:
                return None
            d = r.json()
            lat = lon = 0.0
            loc = d.get("loc") or ""       # ipinfo 形如 "31.2222,121.4581"
            if "," in loc:
                try:
                    a, b = loc.split(",", 1)
                    lat, lon = float(a), float(b)
                except Exception:
                    lat = lon = 0.0
            return {"ip": d.get("ip", ""), "country": (d.get("country") or "").upper(),
                    "lat": lat, "lon": lon, "city": d.get("city", "")}
    except Exception:
        return None
