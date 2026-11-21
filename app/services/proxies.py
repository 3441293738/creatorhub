"""代理输入解析、连通性与出口归属地探测及账号代理分配辅助。"""
from __future__ import annotations

from ipaddress import ip_address
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re

from fastapi import HTTPException
from pydantic import BaseModel, Field as PydanticField

from app.browser.backends import parse_extra_launch_args
from app.browser.proxy import ProxyPlan


class AccountFingerprintUpdateIn(BaseModel):
    seed: str
    source_ip: str = ""
    country: str = ""
    region: str = ""
    city: str = ""
    timezone: str = "Asia/Shanghai"
    locale: str = "zh-CN"
    accept_languages: str = ""
    viewport_w: int = 1280
    viewport_h: int = 800
    geo_lat: float = 0.0
    geo_lon: float = 0.0
    platform: str = ""
    platform_version: str = ""
    brand: str = ""
    brand_version: str = ""
    hardware_concurrency: int = 0
    gpu_vendor: str = ""
    gpu_renderer: str = ""
    disable_spoofing: list[str] = PydanticField(default_factory=list)
    language_mode: str = "auto"
    timezone_mode: str = "auto"
    viewport_mode: str = "auto"
    location_mode: str = "auto"
    geolocation_permission: str = "allow"
    webrtc_mode: str = "conceal"
    extra_args: str = ""


def _validate_fingerprint_update(body: AccountFingerprintUpdateIn) -> dict:
    seed = str(body.seed or "").strip()
    if not seed or len(seed) > 128:
        raise HTTPException(400, "指纹种子长度必须为 1 到 128 个字符")
    source_ip = str(body.source_ip or "").strip()
    if source_ip:
        try:
            source_ip = str(ip_address(source_ip))
        except ValueError as exc:
            raise HTTPException(400, "指纹来源 IP 格式无效") from exc
    timezone_id = str(body.timezone or "").strip()
    try:
        ZoneInfo(timezone_id)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(400, "请输入有效的 IANA 时区") from exc
    locale = str(body.locale or "").strip()
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", locale):
        raise HTTPException(400, "语言格式无效，例如 zh-CN 或 en-US")
    accepted = str(body.accept_languages or "").strip()
    if len(accepted) > 160 or any(ord(char) < 32 for char in accepted):
        raise HTTPException(400, "Accept-Language 格式无效")
    if not 320 <= body.viewport_w <= 7680 \
            or not 240 <= body.viewport_h <= 4320:
        raise HTTPException(400, "窗口尺寸超出允许范围")
    if not -90 <= body.geo_lat <= 90 or not -180 <= body.geo_lon <= 180:
        raise HTTPException(400, "地理坐标超出允许范围")
    platform = str(body.platform or "").strip().lower()
    if platform not in {"", "windows", "linux", "macos"}:
        raise HTTPException(400, "操作系统类型取值无效")
    version_pattern = r"[0-9A-Za-z._-]{0,40}"
    platform_version = str(body.platform_version or "").strip()
    brand_version = str(body.brand_version or "").strip()
    if not re.fullmatch(version_pattern, platform_version) \
            or not re.fullmatch(version_pattern, brand_version):
        raise HTTPException(400, "平台或浏览器版本格式无效")
    brand = str(body.brand or "").strip()
    if len(brand) > 40 or (brand and not re.fullmatch(r"[\w .-]+", brand)):
        raise HTTPException(400, "浏览器品牌格式无效")
    if body.hardware_concurrency != 0 \
            and not 1 <= body.hardware_concurrency <= 256:
        raise HTTPException(400, "CPU 核心数必须为 1 到 256，0 表示自动")
    gpu_vendor = str(body.gpu_vendor or "").strip()
    gpu_renderer = str(body.gpu_renderer or "").strip()
    if len(gpu_vendor) > 160 or len(gpu_renderer) > 240 \
            or any(ord(char) < 32 for char in gpu_vendor + gpu_renderer):
        raise HTTPException(400, "GPU 指纹文本格式无效")
    allowed_spoofing = {"font", "audio", "canvas", "clientrects", "gpu"}
    disabled = []
    for value in body.disable_spoofing:
        name = str(value or "").strip().lower()
        if name not in allowed_spoofing:
            raise HTTPException(400, f"未知指纹模块: {name}")
        if name not in disabled:
            disabled.append(name)
    modes = {
        "fp_language_mode": (body.language_mode, {"auto", "custom"}, "语言模式"),
        "fp_timezone_mode": (body.timezone_mode, {"auto", "custom"}, "时区模式"),
        "fp_viewport_mode": (body.viewport_mode, {"auto", "custom"}, "窗口模式"),
        "fp_location_mode": (body.location_mode, {"auto", "custom"}, "位置模式"),
        "fp_geolocation_permission": (
            body.geolocation_permission, {"ask", "allow", "deny"}, "地理位置权限"),
        "fp_webrtc_mode": (body.webrtc_mode, {"conceal", "allow"}, "WebRTC 模式"),
    }
    normalized_modes = {}
    for field_name, (raw_value, allowed, label) in modes.items():
        selected = str(raw_value or "").strip().lower()
        if selected not in allowed:
            raise HTTPException(400, f"{label}取值无效")
        normalized_modes[field_name] = selected
    extra_args = str(body.extra_args or "").strip()
    try:
        parse_extra_launch_args(extra_args)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    def _short(value: str, field: str) -> str:
        result = str(value or "").strip()
        if len(result) > 100:
            raise HTTPException(400, f"{field}长度超过限制")
        return result
    result = {
        "fp_seed": seed,
        "fp_source_ip": source_ip,
        "fp_country": _short(body.country, "国家/地区"),
        "fp_region": _short(body.region, "区域"),
        "fp_city": _short(body.city, "城市"),
        "timezone_id": timezone_id,
        "locale": locale,
        "fp_accept_languages": accepted,
        "viewport_w": body.viewport_w,
        "viewport_h": body.viewport_h,
        "geo_lat": body.geo_lat,
        "geo_lon": body.geo_lon,
        "fp_platform": platform,
        "fp_platform_version": platform_version,
        "fp_brand": brand,
        "fp_brand_version": brand_version,
        "fp_hardware_concurrency": body.hardware_concurrency,
        "fp_gpu_vendor": gpu_vendor,
        "fp_gpu_renderer": gpu_renderer,
        "fp_disable_spoofing": ",".join(disabled),
        "fp_extra_args": extra_args,
    }
    result.update(normalized_modes)
    return result


def _mask_proxy(proxy: str) -> str:
    """脱敏展示代理(隐藏账号密码)。"""
    if not proxy:
        return ""
    try:
        plan = ProxyPlan.parse(proxy)
        return plan.redacted if plan else ""
    except Exception:
        return "***"


def _parse_ipinfo(j: dict) -> dict:
    lat = lon = 0.0
    loc = str(j.get("loc") or "")
    if "," in loc:
        try:
            lat, lon = (float(value) for value in loc.split(",", 1))
        except (TypeError, ValueError):
            lat = lon = 0.0
    return {"ip": j.get("ip", ""), "country": j.get("country", ""),
            "region": j.get("region", ""), "city": j.get("city", ""),
            "isp": j.get("org", ""), "timezone": j.get("timezone", ""),
            "lat": lat, "lon": lon}


def _parse_ipapi(j: dict) -> dict:
    if j.get("status") != "success":
        return {}
    return {"ip": j.get("query", ""),
            "country": j.get("countryCode") or j.get("country", ""),
            "region": j.get("regionName", ""), "city": j.get("city", ""),
            "isp": j.get("isp", ""), "timezone": j.get("timezone", ""),
            "lat": j.get("lat") or 0.0, "lon": j.get("lon") or 0.0}


async def _proxy_geo(proxy_url: str, timeout: float = 8) -> dict | None:
    """经代理查出口 IP/归属地/时区/坐标；空代理表示宿主直连。"""
    import httpx
    sources = [
        ("https://ipinfo.io/json", _parse_ipinfo),
        ("http://ip-api.com/json/?lang=zh-CN&fields=status,country,countryCode,regionName,city,lat,lon,timezone,isp,query",
         _parse_ipapi),
    ]
    try:
        client_options = {"timeout": timeout, "follow_redirects": True}
        if proxy_url:
            client_options["proxy"] = proxy_url
        async with httpx.AsyncClient(**client_options) as cli:
            for url, parser in sources:
                try:
                    g = parser((await cli.get(url)).json())
                    if g and g.get("ip"):
                        return g
                except Exception:
                    continue
    except Exception:
        pass
    return None
