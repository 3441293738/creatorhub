"""Deterministic account fingerprints aligned with a network exit IP."""
from __future__ import annotations

import hashlib
import ipaddress
import random
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .identity import DEFAULT_LOCALE, DEFAULT_TZ, VIEWPORTS


_COUNTRY_ALIASES = {
    "中国": "CN", "china": "CN", "中国香港": "HK", "香港": "HK",
    "hong kong": "HK", "中国澳门": "MO", "澳门": "MO", "澳門": "MO",
    "macau": "MO", "中国台湾": "TW", "台湾": "TW", "台灣": "TW",
    "taiwan": "TW", "united states": "US", "united kingdom": "GB",
    "japan": "JP", "south korea": "KR", "korea": "KR",
    "singapore": "SG", "germany": "DE", "france": "FR",
    "canada": "CA", "australia": "AU", "new zealand": "NZ",
}

_LOCALE_BY_COUNTRY = {
    "CN": "zh-CN", "HK": "zh-HK", "MO": "zh-MO", "TW": "zh-TW",
    "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU",
    "NZ": "en-NZ", "IE": "en-IE", "SG": "en-SG", "IN": "en-IN",
    "JP": "ja-JP", "KR": "ko-KR", "DE": "de-DE", "FR": "fr-FR",
    "ES": "es-ES", "IT": "it-IT", "PT": "pt-PT", "BR": "pt-BR",
    "MX": "es-MX", "AR": "es-AR", "NL": "nl-NL", "BE": "nl-BE",
    "CH": "de-CH", "AT": "de-AT", "PL": "pl-PL", "CZ": "cs-CZ",
    "SE": "sv-SE", "NO": "nb-NO", "DK": "da-DK", "FI": "fi-FI",
    "RU": "ru-RU", "UA": "uk-UA", "TR": "tr-TR", "SA": "ar-SA",
    "AE": "ar-AE", "IL": "he-IL", "TH": "th-TH", "VN": "vi-VN",
    "ID": "id-ID", "MY": "ms-MY", "PH": "en-PH",
}

_TIMEZONE_BY_COUNTRY = {
    "CN": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "MO": "Asia/Macau",
    "TW": "Asia/Taipei", "JP": "Asia/Tokyo", "KR": "Asia/Seoul",
    "SG": "Asia/Singapore", "MY": "Asia/Kuala_Lumpur",
    "TH": "Asia/Bangkok", "VN": "Asia/Ho_Chi_Minh",
    "ID": "Asia/Jakarta", "PH": "Asia/Manila", "IN": "Asia/Kolkata",
    "GB": "Europe/London", "IE": "Europe/Dublin", "DE": "Europe/Berlin",
    "FR": "Europe/Paris", "ES": "Europe/Madrid", "IT": "Europe/Rome",
    "PT": "Europe/Lisbon", "NL": "Europe/Amsterdam", "BE": "Europe/Brussels",
    "CH": "Europe/Zurich", "AT": "Europe/Vienna", "PL": "Europe/Warsaw",
    "CZ": "Europe/Prague", "SE": "Europe/Stockholm", "NO": "Europe/Oslo",
    "DK": "Europe/Copenhagen", "FI": "Europe/Helsinki", "RU": "Europe/Moscow",
    "UA": "Europe/Kyiv", "TR": "Europe/Istanbul", "US": "America/New_York",
    "CA": "America/Toronto", "MX": "America/Mexico_City",
    "BR": "America/Sao_Paulo", "AR": "America/Argentina/Buenos_Aires",
    "AU": "Australia/Sydney", "NZ": "Pacific/Auckland", "SA": "Asia/Riyadh",
    "AE": "Asia/Dubai", "IL": "Asia/Jerusalem",
}


def normalize_country(country: str) -> str:
    value = str(country or "").strip()
    if not value:
        return ""
    if len(value) == 2 and value.isascii() and value.isalpha():
        return value.upper()
    return _COUNTRY_ALIASES.get(value.lower(), _COUNTRY_ALIASES.get(value, ""))


def locale_for_country(country: str, fallback: str = DEFAULT_LOCALE) -> str:
    return _LOCALE_BY_COUNTRY.get(normalize_country(country), fallback or DEFAULT_LOCALE)


def _valid_timezone(value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return ""
    return candidate


def timezone_for_geo(
    country: str,
    timezone_id: str = "",
    fallback: str = DEFAULT_TZ,
) -> str:
    explicit = _valid_timezone(timezone_id)
    if explicit:
        return explicit
    mapped = _valid_timezone(_TIMEZONE_BY_COUNTRY.get(normalize_country(country), ""))
    return mapped or _valid_timezone(fallback) or DEFAULT_TZ


def _canonical_ip(value: str) -> str:
    try:
        return ipaddress.ip_address(str(value or "").strip()).compressed
    except ValueError as exc:
        raise ValueError("出口 IP 无效") from exc


def derive_ip_fingerprint(
    account_key: object,
    ip: str,
    *,
    country: str = "",
    region: str = "",
    city: str = "",
    timezone_id: str = "",
    latitude: float = 0.0,
    longitude: float = 0.0,
    fallback_timezone: str = DEFAULT_TZ,
    fallback_locale: str = DEFAULT_LOCALE,
) -> dict:
    """Return a stable, per-account fingerprint aligned with an exit IP.

    The IP determines the network locality while ``account_key`` prevents two
    accounts sharing one NAT/proxy exit from receiving an identical device.
    """
    canonical_ip = _canonical_ip(ip)
    country_code = normalize_country(country)
    material = f"creatorhub-ip-fingerprint-v1|{account_key}|{canonical_ip}"
    seed = hashlib.sha256(material.encode("utf-8")).hexdigest()
    rnd = random.Random(seed)
    width, height = rnd.choice(VIEWPORTS)

    try:
        lat, lon = float(latitude or 0.0), float(longitude or 0.0)
    except (TypeError, ValueError):
        lat = lon = 0.0
    if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
        # Geo providers commonly return a shared city-centre point.  A tiny,
        # deterministic offset keeps accounts distinct without leaving the city.
        lat = round(max(-90, min(90, lat + rnd.uniform(-0.012, 0.012))), 6)
        lon = round(max(-180, min(180, lon + rnd.uniform(-0.012, 0.012))), 6)
    else:
        lat = lon = 0.0

    return {
        "fp_seed": seed,
        "fingerprint_id": seed[:12],
        "source_ip": canonical_ip,
        "country": country_code,
        "region": str(region or "").strip(),
        "city": str(city or "").strip(),
        "timezone_id": timezone_for_geo(
            country_code, timezone_id, fallback_timezone),
        "locale": locale_for_country(country_code, fallback_locale),
        "viewport_w": width,
        "viewport_h": height,
        "geo_lat": lat,
        "geo_lon": lon,
    }
