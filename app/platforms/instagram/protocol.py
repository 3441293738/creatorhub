"""Instagram 协议原语、设备画像、会话状态、签名/表单编码与 TOTP/Breadcrumb 算法。

零网络依赖，完整区分四类请求编码、双栈会话路径与确定性设备标识派生：
- Android Mobile API (`i.instagram.com/api/v1/`): `Bearer IGT:2:` + `X-Ig-*` 头 + `SIGNATURE.` / 无签名前缀 / `%20` configure 编码
- Web 浏览器会话 HTTP (`www.instagram.com` / `i.instagram.com`): `Cookie` + `X-CSRFToken` + `X-Ig-App-Id: 936619743392459`
- iOS 传统签名辅助: UTF-8 密钥文本 HMAC-SHA256
- 密码外层 Base64 封装辅助结构
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import random
import re
import struct
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, quote_plus, unquote
from zoneinfo import ZoneInfo

# ── 基础地址与客户端常量 ──
BASEAPI_URL = "https://i.instagram.com/api/v1/"
BASEAPI_URL_TRIMMED = "https://i.instagram.com/api/v1"
BASEAPI_URL_V2 = "https://i.instagram.com/api/v2/"
BASEAPI_URL_NEW = "https://b.i.instagram.com/api/v1/"
RUPLOAD_PHOTO_URL = "https://i.instagram.com/rupload_igphoto/"
RUPLOAD_VIDEO_URL = "https://i.instagram.com/rupload_igvideo/"
WWW_BASE = "https://www.instagram.com"

MOBILE_APP_ID = "567067343352427"
WEB_APP_ID = "936619743392459"
WEB_AJAX_ID = "1006630603"
WEB_ASBD_ID = "129477"
X_IG_CAPABILITIES = "3brTv10="
APP_VERSION = "305.0.0.34.110"
VERSION_CODE = "527673528"
BLOKS_VERSION_ID = "d0955a960892f261250588fcb273283b0b683121a6a6ca19202256cef6e3c22b"

IOS_LEGACY_KEY_TEXT = "607d66fdb62e59094257ed48a3e15b4860db7b87c7174dd8320f668d463f1621"
BREADCRUMB_KEY = b"iN4$aGr0m"
RETRY_CONTEXT_DEFAULT = '{"num_reupload":0,"num_step_auto_retry":0,"num_step_manual_retry":0}'

SUPPORTED_CAPABILITIES_JSON = json.dumps(
    [
        {
            "name": "SUPPORTED_SDK_VERSIONS",
            "value": ",".join(f"{n}.0" for n in range(133, 173)),
        },
        {"name": "FACE_TRACKER_VERSION", "value": "14"},
        {"name": "segmentation", "value": "segmentation_enabled"},
        {"name": "COMPRESSION", "value": "ETC2_COMPRESSION"},
        {"name": "gyroscope", "value": "gyroscope_enabled"},
    ],
    separators=(",", ":"),
)

# 典型 8 段 Android 设备 UA 列表
ANDROID_DEVICE_UAS: Tuple[str, ...] = (
    "28/9; 420dpi; 1080x2042; samsung; SM-G973F; beyond1; exynos9820; en_US",
    "28/9; 420dpi; 1080x2047; samsung; SM-G975F; beyond2; exynos9820; en_US",
    "28/9; 420dpi; 1080x2064; samsung; SM-N970F; d1; exynos9825; en_US",
    "28/9; 420dpi; 1080x2135; OnePlus; ONEPLUS A6013; OnePlus6T; qcom; en_US",
    "28/9; 420dpi; 1080x2134; OnePlus; GM1900; OnePlus7; qcom; en_US",
    "28/9; 440dpi; 1080x2028; Google/google; Pixel 3; blueline; blueline; en_US",
    "28/9; 440dpi; 1080x2135; Xiaomi; MI 9; cepheus; qcom; en_US",
    "28/9; 440dpi; 1080x2210; Xiaomi; Mi 9T Pro; raphael; qcom; en_US",
    "28/9; 480dpi; 1080x2049; HUAWEI; HMA-L29; HWHMA; kirin980; en_US",
    "28/9; 420dpi; 1080x2198; samsung; SM-A705FN; a70q; qcom; en_US",
)

# 公元 0001-01-01 至 Unix Epoch (1970-01-01) 的 100ns 计数偏移
_EPOCH_100NS_OFFSET = 621355968000000000
_TOTP_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _seeded_uuid(seed: str, purpose: str) -> str:
    """按账号种子与用途确定性派生独立 UUID，保证 UniqueId / UniqueIdEx / FamilyDeviceGuid 等互不相同。"""
    digest = hmac.new(
        f"creatorhub-ig-{purpose}".encode("utf-8"),
        seed.encode("utf-8"),
        hashlib.sha256,
    ).digest()[:16]
    b = bytearray(digest)
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


def derive_device_id(unique_id: str, explicit: Optional[str] = None) -> str:
    """生成 Android 设备标识: `android-` + UUID 去横杠前 16 位十六进制。"""
    if explicit:
        return explicit
    normalized = str(uuid.UUID(str(unique_id))).replace("-", "")
    return "android-" + normalized[:16]


def is_valid_android_device_ua(device_ua: str) -> bool:
    """校验 Android 设备描述串格式: `; ` 分割为 8 段且首段含 `/` 版本号。"""
    if not device_ua or not isinstance(device_ua, str):
        return False
    parts = device_ua.split("; ")
    if len(parts) != 8:
        return False
    first = device_ua.split(";")[0].strip().split("/")
    return len(first) == 2 and all(first)


def pick_android_device_ua(seed: str = "", locale: str = "") -> str:
    if not seed:
        base = ANDROID_DEVICE_UAS[0]
    else:
        idx = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:4], "big") % len(ANDROID_DEVICE_UAS)
        base = ANDROID_DEVICE_UAS[idx]
    return _align_android_ua_locale(base, locale)


def _align_android_ua_locale(device_ua: str, locale: str) -> str:
    loc = str(locale or "").strip().replace("-", "_")
    if not loc or not is_valid_android_device_ua(device_ua):
        return device_ua
    parts = device_ua.split("; ")
    parts[7] = loc
    return "; ".join(parts)


_IG_TZ_GEO_MAP: Dict[str, Tuple[str, str]] = {
    "America/New_York": ("US", "en_US"),
    "America/Chicago": ("US", "en_US"),
    "America/Denver": ("US", "en_US"),
    "America/Los_Angeles": ("US", "en_US"),
    "America/Phoenix": ("US", "en_US"),
    "America/Anchorage": ("US", "en_US"),
    "Pacific/Honolulu": ("US", "en_US"),
    "America/Toronto": ("CA", "en_CA"),
    "America/Vancouver": ("CA", "en_CA"),
    "America/Sao_Paulo": ("BR", "pt_BR"),
    "America/Mexico_City": ("MX", "es_MX"),
    "Europe/London": ("GB", "en_GB"),
    "Europe/Berlin": ("DE", "de_DE"),
    "Europe/Paris": ("FR", "fr_FR"),
    "Europe/Madrid": ("ES", "es_ES"),
    "Europe/Rome": ("IT", "it_IT"),
    "Australia/Sydney": ("AU", "en_AU"),
    "Australia/Melbourne": ("AU", "en_AU"),
    "Asia/Singapore": ("SG", "en_SG"),
    "Asia/Shanghai": ("SG", "en_SG"),
    "Asia/Chongqing": ("SG", "en_SG"),
    "Asia/Harbin": ("SG", "en_SG"),
    "Asia/Urumqi": ("SG", "en_SG"),
    "PRC": ("SG", "en_SG"),
    "Asia/Hong_Kong": ("HK", "zh_HK"),
    "Asia/Taipei": ("TW", "zh_TW"),
    "Asia/Tokyo": ("JP", "ja_JP"),
    "Asia/Seoul": ("KR", "ko_KR"),
    "Asia/Manila": ("PH", "en_PH"),
    "Asia/Kuala_Lumpur": ("MY", "ms_MY"),
    "Asia/Bangkok": ("TH", "th_TH"),
    "Asia/Jakarta": ("ID", "id_ID"),
    "Asia/Ho_Chi_Minh": ("VN", "vi_VN"),
    "Asia/Saigon": ("VN", "vi_VN"),
    "Asia/Kolkata": ("IN", "en_IN"),
    "Asia/Calcutta": ("IN", "en_IN"),
}

_IG_COUNTRY_DEFAULT_LOCALE: Dict[str, str] = {
    "US": "en_US",
    "CA": "en_CA",
    "GB": "en_GB",
    "AU": "en_AU",
    "SG": "en_SG",
    "HK": "zh_HK",
    "TW": "zh_TW",
    "JP": "ja_JP",
    "KR": "ko_KR",
    "PH": "en_PH",
    "MY": "ms_MY",
    "TH": "th_TH",
    "ID": "id_ID",
    "VN": "vi_VN",
    "IN": "en_IN",
    "DE": "de_DE",
    "FR": "fr_FR",
    "ES": "es_ES",
    "IT": "it_IT",
    "BR": "pt_BR",
    "MX": "es_MX",
}


def resolve_ig_timezone_geo(
    timezone_id: str = "",
    *,
    timezone_offset: Optional[int] = None,
    country_iso_code: str = "",
    locale: str = "",
) -> Tuple[str, str, int]:
    """对齐 `(country_iso_code, locale, timezone_offset)`，杜绝 `UTC+8 (28800)` 与 `en_US`/`US` 混搭。"""
    tz_clean = str(timezone_id or "").strip()
    if tz_clean:
        off = timezone_offset_seconds(tz_clean)
    elif timezone_offset is not None:
        off = int(timezone_offset)
    else:
        off = 28800

    is_us_tz = tz_clean.startswith(("America/New_York", "America/Chicago", "America/Denver",
                                    "America/Los_Angeles", "America/Phoenix", "America/Anchorage",
                                    "Pacific/Honolulu", "US/")) or (-36000 <= off <= -14400 and not tz_clean)

    mapped_country, mapped_locale = "", ""
    if tz_clean in _IG_TZ_GEO_MAP:
        mapped_country, mapped_locale = _IG_TZ_GEO_MAP[tz_clean]
    elif is_us_tz:
        mapped_country, mapped_locale = "US", "en_US"
    elif off == 28800:
        mapped_country, mapped_locale = "SG", "en_SG"
    elif off == 32400:
        mapped_country, mapped_locale = "JP", "ja_JP"
    elif off == 25200:
        mapped_country, mapped_locale = "TH", "th_TH"
    elif off in {0, 3600}:
        mapped_country, mapped_locale = "GB", "en_GB"
    else:
        mapped_country, mapped_locale = "GB", "en_GB"

    explicit_country = str(country_iso_code or "").strip().upper()
    if explicit_country and explicit_country != "CN":
        if explicit_country == "US" and not is_us_tz:
            country = mapped_country
        else:
            country = explicit_country
    else:
        country = mapped_country

    explicit_locale = str(locale or "").strip().replace("-", "_")
    if explicit_locale and not explicit_locale.lower().startswith("zh_cn"):
        if explicit_locale == "en_US" and not is_us_tz:
            loc = _IG_COUNTRY_DEFAULT_LOCALE.get(country, mapped_locale)
        else:
            loc = explicit_locale
    else:
        loc = _IG_COUNTRY_DEFAULT_LOCALE.get(country, mapped_locale)

    return country, loc, off


def _derive_ig_account_seed(
    account_seed: str = "",
    cookies: Optional[Mapping[str, str]] = None,
    *,
    ds_user_id: str = "",
    sessionid: str = "",
    mid: str = "",
) -> str:
    """确保 `account_seed` 为空时也能从 `ds_user_id` / `sessionid` 派生跨进程确定性种子。"""
    explicit = str(account_seed or "").strip()
    if explicit:
        return explicit
    cks = cookies or {}
    uid = str(ds_user_id or cks.get("ds_user_id") or "").strip()
    sess = str(sessionid or cks.get("sessionid") or "").strip()
    if not uid and sess:
        head = unquote(sess).split(":", 1)[0].strip()
        if head.isdigit():
            uid = head
    mid_val = str(mid or cks.get("mid") or "").strip()
    material = uid or sess or mid_val or "ig-anonymous"
    return hashlib.sha256(f"ig-acct-seed|{material}".encode("utf-8")).hexdigest()[:32]


def parse_android_device_ua(device_ua: str) -> Dict[str, str]:
    """解析 8 段 Android 设备描述串为标准设备元数据字典。"""
    ua = device_ua if is_valid_android_device_ua(device_ua) else ANDROID_DEVICE_UAS[0]
    parts = [p.strip() for p in ua.split(";")]
    ver_parts = [p.strip() for p in parts[0].split("/")]
    model = parts[4] if "-" in parts[4] else parts[5]
    return {
        "manufacturer": parts[3],
        "model": model,
        "android_version": ver_parts[0],
        "android_release": ver_parts[1],
    }


def full_mobile_user_agent(device_ua: str) -> str:
    """拼接完整的 Instagram Android 客户端 User-Agent。"""
    ua = device_ua if is_valid_android_device_ua(device_ua) else ANDROID_DEVICE_UAS[0]
    return f"Instagram {APP_VERSION} Android ({ua}; {VERSION_CODE})"


def timezone_offset_seconds(timezone_id: str = "Asia/Shanghai") -> int:
    tz_name = str(timezone_id or "").strip() or "Asia/Shanghai"
    try:
        tz = ZoneInfo(tz_name)
        off = datetime.now(tz).utcoffset()
        if off is not None:
            return int(off.total_seconds())
    except Exception:
        pass
    return 28800


def get_priority(url: str) -> str:
    """根据请求路径计算 HTTP `Priority` 头等级。"""
    u = str(url or "")
    if "feed/reels_tray" in u or "feed/timeline" in u:
        return "0"
    if "/api/" in u:
        return "3"
    return "6, i"


def format_raw_client_time(timestamp_ms: Optional[int] = None) -> str:
    ms = int(timestamp_ms if timestamp_ms and timestamp_ms > 0 else round(time.time() * 1000))
    return f"{ms / 1000.0:.3f}"


# ── 请求编码与签名原语 ──

def form_encode(pairs: Iterable[Tuple[Any, Any]]) -> str:
    """标准 URL 表单编码（空格转为 `+`，保留 `~`）。"""
    return "&".join(
        quote_plus(str(k), safe="~") + "=" + quote_plus("" if v is None else str(v), safe="~")
        for k, v in pairs
    )


def form_value_to_string(value: Any) -> str:
    """无签名表单字段字符串化（布尔值转为首字母大写 `True`/`False`，`None` 映射为空串）。"""
    if value is None:
        return ""
    if value is True:
        return "True"
    if value is False:
        return "False"
    return str(value)


def content_from_serialized_json(
    serialized_json: str,
    field_name: str = "signed_body",
    fixed_prefix: str = "SIGNATURE.",
    additional: Sequence[Tuple[str, Any]] = (),
) -> bytes:
    """构造 `signed_body=SIGNATURE.<json>` 表单编码请求体。"""
    pairs: List[Tuple[Any, Any]] = [(field_name, fixed_prefix + serialized_json), *additional]
    return form_encode(pairs).encode("utf-8")


def signed_dict_content(
    payload: Mapping[str, Any],
    additional: Sequence[Tuple[str, Any]] = (),
) -> bytes:
    exact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return content_from_serialized_json(exact, additional=additional)


def large_signed_content(serialized_json: str) -> bytes:
    """直接拼接未转义 JSON 的大负载 `signed_body=SIGNATURE.` 请求体。"""
    return ("signed_body=SIGNATURE." + serialized_json).encode("utf-8")


def unsigned_content(pairs: Iterable[Tuple[str, Any]] | Mapping[str, Any]) -> bytes:
    """构造不带 `SIGNATURE.` 前缀的纯表单请求体。"""
    items = pairs.items() if isinstance(pairs, Mapping) else pairs
    normalized = [(str(k), form_value_to_string(v)) for k, v in items]
    return form_encode(normalized).encode("utf-8")


def ig_configure_content(exact_json: str) -> bytes:
    """构造媒体 `configure` 请求体：对 JSON 做百分号编码（空格为 `%20` 而非 `+`），
    直接拼接 `signed_body=SIGNATURE.` 前缀，避免 `%` 被二次编码为 `%25`。
    """
    return ("signed_body=SIGNATURE." + quote(exact_json, safe="-._~")).encode("ascii")


def ios_signature(exact_json_text: str, key_text: str = IOS_LEGACY_KEY_TEXT) -> str:
    """使用 UTF-8 密钥文本计算 HMAC-SHA256 小写十六进制摘要。"""
    return hmac.new(
        key_text.encode("utf-8"),
        exact_json_text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def password_service_envelope(password: str, random_prefix_30: str, k: str, v: str) -> Dict[str, str]:
    """构造多层 Base64 密码传输外层封装字典。"""
    if len(random_prefix_30) != 30:
        raise ValueError("random prefix must be 30 characters")
    text = password
    for _ in range(5):
        text = base64.b64encode(text.encode("utf-8")).decode("ascii")
    ident = base64.b64encode((random_prefix_30 + text).encode("utf-8")).decode("ascii")
    return {
        "ident": ident,
        "connection": k,
        "res": base64.b64encode(v.encode("utf-8")).decode("ascii"),
    }


# ── 媒体时长、缓冲、等待与评论 Breadcrumb / UniqueTimeId / TOTP ──

def ig_video_duration_values(seconds: float) -> Dict[str, int]:
    """计算视频上传头 (`ceil`) 与 configure 元数据 (`int`) 所需的两套毫秒时长。"""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("finite nonnegative duration required")
    return {
        "rupload_upload_media_duration_ms": int(math.ceil(seconds * 1000)),
        "metadata_DurationInMS": int(seconds * 1000),
    }


def ig_legacy_stream_buffer(file_bytes: int) -> int:
    if isinstance(file_bytes, bool) or not isinstance(file_bytes, int) or file_bytes < 0:
        raise ValueError("nonnegative size required")
    return math.ceil(file_bytes / 4)


def ig_legacy_session_id(upload_id: str) -> str:
    digest = hashlib.md5(str(upload_id).encode("utf-8")).hexdigest().upper()
    return f"{upload_id}-{digest}"


def ig_thumbnail_wait_ms(file_bytes: int) -> int:
    """根据视频字节数计算封面上传后的转码缓冲等待毫秒数。"""
    if isinstance(file_bytes, bool) or not isinstance(file_bytes, int) or file_bytes < 0:
        raise ValueError("nonnegative size required")
    return max(7000, int(file_bytes / 1024.0) * 2)


def ig_comment_breadcrumb(
    comment: str,
    word_count: int,
    typing_ms_per_word: int,
    burst_count: int,
    local_datetime_ticks: int,
) -> str:
    """计算评论输入轨迹签名 `user_breadcrumb`（按 UTF-16 代码单元计数并使用 HMAC-SHA256 签名）。"""
    if not 800 <= typing_ms_per_word < 1200:
        raise ValueError("typing random outside target range")
    if burst_count not in (1, 2):
        raise ValueError("burst random outside target range")
    if word_count < 0 or local_datetime_ticks < 0:
        raise ValueError("nonnegative metrics required")
    utf16_len = len(comment.encode("utf-16-le", errors="surrogatepass")) // 2
    data = f"{utf16_len} {word_count * typing_ms_per_word} {burst_count} {local_datetime_ticks // 10000}"
    sig = base64.b64encode(hmac.new(BREADCRUMB_KEY, data.encode("ascii"), hashlib.sha256).digest()).decode("ascii")
    encoded = base64.b64encode(data.encode("utf-8")).decode("ascii")
    return f"{sig}\n{encoded}\n"


def generate_comment_breadcrumb(comment: str, *, now_ms: Optional[int] = None) -> str:
    """运行期生成合法的评论 `user_breadcrumb`。"""
    words = max(1, len([w for w in str(comment or "").split() if w]))
    typing_ms = random.randrange(800, 1200)
    burst = random.randrange(1, 3)
    epoch_ms = int(now_ms if now_ms is not None else round(time.time() * 1000))
    ticks = _EPOCH_100NS_OFFSET + epoch_ms * 10000
    return ig_comment_breadcrumb(comment, words, typing_ms, burst, ticks)


def unique_time_id(time_ms: int = 0, rand_low22: Optional[int] = None) -> int:
    """生成 63 位单调递增客户端消息幂等标识，用于 Direct v2 `client_context` / `mutation_token` / `offline_threading_id`。"""
    ms = int(time_ms if time_ms > 0 else round(time.time() * 1000))
    low = (random.getrandbits(22) if rand_low22 is None else int(rand_low22)) & 0x3FFFFF
    return ((ms << 22) | low) & 0x7FFFFFFFFFFFFFFF


def generate_upload_id(*, use_new: bool = False, time_ms: int = 0) -> str:
    """生成媒体上传 `upload_id`。"""
    if use_new:
        return str(unique_time_id(time_ms=time_ms) // 10014)
    ms = int(time_ms if time_ms > 0 else round(time.time() * 1000))
    return str(ms)


def totp_base32_decode_strict(secret_key: str) -> bytes:
    """严格解码无空格大写 Base32 (`A-Z2-7`) 密钥。"""
    acc = 0
    bits = 0
    out = bytearray()
    for ch in secret_key:
        idx = _TOTP_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError("Invalid character in Base32 secret")
        acc = ((acc << 5) | idx) & 0xFFFFFFFF
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return bytes(out)


def totp_token_at(secret_key: str, unix_seconds: Optional[int] = None) -> str:
    """基于 RFC 6238 (HMAC-SHA1, 30 秒步长) 生成 6 位 TOTP 动态口令。"""
    sec = int(time.time()) if unix_seconds is None else unix_seconds
    if isinstance(sec, bool) or not isinstance(sec, int) or sec < 0:
        raise ValueError("nonnegative integer Unix seconds required")
    cleaned = str(secret_key or "").replace(" ", "")
    counter = sec // 30
    digest = hmac.new(totp_base32_decode_strict(cleaned), struct.pack(">q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    val = int.from_bytes(digest[off:off + 4], "big") & 0x7FFFFFFF
    return f"{val % 1_000_000:06d}"


# ── 分页 RankToken 缓存与会话状态 ──

class RankTokenCache:
    """关注/粉丝分页 `rank_token` 缓存：首屏(`max_id=None`)重建，翻页复用；容量 >200 时淘汰前 50 项。"""

    def __init__(self, guid_factory: Callable[[], str] = lambda: str(uuid.uuid4())):
        self.tokens: Dict[str, str] = {}
        self.guid_factory = guid_factory

    def get(self, ds_user_id: str, user_id: str, max_id: Optional[str], ranked: bool = False) -> str:
        if len(self.tokens) > 200:
            for k in list(self.tokens)[:50]:
                del self.tokens[k]
        key = f"{ds_user_id or ''}{user_id or ''}{'ranked' if ranked else ''}"
        if not max_id:
            self.tokens.pop(key, None)
        if key not in self.tokens:
            self.tokens[key] = self.guid_factory()
        return self.tokens[key]


class HashtagRankTokenCache:
    """话题搜索 `rank_token` 缓存：容量 >500 时淘汰前 50 项。"""

    def __init__(self, guid_factory: Callable[[], str] = lambda: str(uuid.uuid4())):
        self.tokens: Dict[str, str] = {}
        self.guid_factory = guid_factory

    def get(self, ds_user_id: str, term: str, max_id: Optional[str], tab_name: str = "recent") -> str:
        if len(self.tokens) > 500:
            for k in list(self.tokens)[:50]:
                del self.tokens[k]
        suffix = "" if tab_name == "recent" else str(tab_name or "")
        key = f"{ds_user_id or ''}{term or ''}{suffix}"
        if not max_id:
            self.tokens.pop(key, None)
        if key not in self.tokens:
            self.tokens[key] = self.guid_factory()
        return self.tokens[key]


@dataclass
class SuspendedOp:
    suspended_until: float
    last_seconds: int
    consecutive_errors: int = 1

    def suspend_more(self) -> None:
        self.consecutive_errors += 1
        step = min(3600, max(30, self.last_seconds * 2))
        self.last_seconds = step
        self.suspended_until = time.time() + step


@dataclass
class InstagramSessionState:
    """单账号隔离的 Instagram 双栈会话与设备状态对象。"""

    seed: str = ""
    unique_id: str = ""
    unique_id_ex: str = ""
    family_device_guid: str = ""
    pigeon_session_id: str = ""
    attribution_id: str = ""
    client_session_id: str = ""
    explicit_device_id: Optional[str] = None
    android_device_ua: str = ""
    web_user_agent: str = ""
    timezone_id: str = ""
    country_iso_code: str = ""
    locale: str = ""
    timezone_offset: int = 28800
    new_domain: bool = False
    ignore_nav_chain: bool = False
    nav_chain: str = ""

    # 认证与服务端写回状态
    auth_sessionid: str = ""
    auth_ds_user_id: str = ""
    auth_mid: str = ""
    auth_bearer: str = ""
    csrf_token: str = ""
    rur: str = ""
    region_hint: str = ""
    shb_id: str = ""
    shb_ts: str = ""
    claims: str = "0"
    enc_key_id: str = ""
    enc_pub_key: str = ""
    seq_id: int = 0
    browser_cookies: Dict[str, str] = field(default_factory=dict)

    # 遥测与分页/限流缓存
    _bandwidth_cache: Optional[Dict[str, str]] = field(default=None, repr=False)
    _bandwidth_window_ms: int = field(default=0, repr=False)
    _last_request_ts_ms: int = field(default=0, repr=False)
    rank_tokens: RankTokenCache = field(default_factory=RankTokenCache, repr=False)
    hashtag_rank_tokens: HashtagRankTokenCache = field(default_factory=HashtagRankTokenCache, repr=False)
    suspended_ops: Dict[str, SuspendedOp] = field(default_factory=dict, repr=False)
    web_suspended_ops: Dict[str, SuspendedOp] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        resolved_country, resolved_locale, resolved_offset = resolve_ig_timezone_geo(
            self.timezone_id,
            timezone_offset=self.timezone_offset,
            country_iso_code=self.country_iso_code,
            locale=self.locale,
        )
        self.country_iso_code = resolved_country
        self.locale = resolved_locale
        self.timezone_offset = resolved_offset

        base_seed = _derive_ig_account_seed(
            self.seed,
            self.browser_cookies,
            ds_user_id=self.auth_ds_user_id,
            sessionid=self.auth_sessionid,
            mid=self.auth_mid,
        )
        self.seed = base_seed
        if not self.unique_id:
            self.unique_id = _seeded_uuid(base_seed, "unique_id")
        if not self.unique_id_ex:
            self.unique_id_ex = _seeded_uuid(base_seed, "unique_id_ex")
        if not self.family_device_guid:
            self.family_device_guid = _seeded_uuid(base_seed, "family_device")
        if not self.pigeon_session_id:
            self.pigeon_session_id = _seeded_uuid(base_seed, "pigeon_session")
        if not self.attribution_id:
            self.attribution_id = _seeded_uuid(base_seed, "attribution")
        if not self.client_session_id:
            self.client_session_id = _seeded_uuid(base_seed, "client_session")
        if not is_valid_android_device_ua(self.android_device_ua):
            self.android_device_ua = pick_android_device_ua(base_seed, self.locale)
        elif self.android_device_ua in ANDROID_DEVICE_UAS:
            self.android_device_ua = _align_android_ua_locale(self.android_device_ua, self.locale)

    @classmethod
    def from_storage_state(
        cls,
        storage_state: str | Mapping[str, Any] | None = None,
        *,
        account_seed: str = "",
        timezone_id: str = "Asia/Shanghai",
        android_device_ua: str = "",
        web_user_agent: str = "",
        new_domain: bool = False,
        locale: str = "",
        country_iso_code: str = "",
    ) -> "InstagramSessionState":
        from .extract import cookie_map_from_state

        cookies = cookie_map_from_state(storage_state)
        det_seed = _derive_ig_account_seed(account_seed, cookies)
        state = cls(
            seed=det_seed,
            android_device_ua=android_device_ua,
            web_user_agent=web_user_agent,
            timezone_id=timezone_id,
            country_iso_code=country_iso_code,
            locale=locale,
            timezone_offset=timezone_offset_seconds(timezone_id),
            new_domain=new_domain,
        )
        state.import_browser_cookies(cookies)
        return state

    @property
    def device_id(self) -> str:
        return derive_device_id(self.unique_id, self.explicit_device_id)

    @property
    def android_device_id(self) -> str:
        return self.device_id

    @property
    def uuid(self) -> str:
        return self.unique_id_ex

    @property
    def phone_id(self) -> str:
        return self.family_device_guid

    @property
    def family_device_id(self) -> str:
        return self.family_device_guid

    @property
    def is_logged_in(self) -> bool:
        return bool(self.auth_sessionid or self.auth_bearer)

    def mobile_user_agent(self) -> str:
        return full_mobile_user_agent(self.android_device_ua)

    def device_data(self) -> Dict[str, str]:
        return parse_android_device_ua(self.android_device_ua)

    def import_browser_cookies(self, cookies: Mapping[str, str]) -> None:
        """从浏览器 Cookie 字典导入并桥接移动端鉴权字段。"""
        if not cookies:
            return
        self.browser_cookies.update({str(k): str(v) for k, v in cookies.items() if k and v is not None})
        if cookies.get("sessionid"):
            self.auth_sessionid = str(cookies["sessionid"])
        if cookies.get("ds_user_id"):
            self.auth_ds_user_id = str(cookies["ds_user_id"])
        if cookies.get("mid"):
            self.auth_mid = str(cookies["mid"])
        if cookies.get("shbid"):
            self.shb_id = str(cookies["shbid"]).strip('"')
        if cookies.get("shbts"):
            self.shb_ts = str(cookies["shbts"]).strip('"')
        if cookies.get("rur"):
            self.rur = str(cookies["rur"]).strip('"')
        if cookies.get("csrftoken"):
            self.csrf_token = str(cookies["csrftoken"])

    def authorization_cookie(self) -> Optional[str]:
        """构造 `Bearer IGT:2:` 鉴权头：优先使用响应头下发的令牌，否则由 `ds_user_id` 与 `sessionid` Base64 编码生成。"""
        if not self.auth_sessionid and not self.auth_bearer:
            return None
        if self.auth_bearer:
            bearer = self.auth_bearer
            if bearer.startswith("Bearer "):
                return bearer
            return "Bearer IGT:2:" + bearer
        raw_json = f'{{"ds_user_id":"{self.auth_ds_user_id}","sessionid":"{self.auth_sessionid}"}}'
        encoded = base64.b64encode(raw_json.encode("utf-8")).decode("ascii")
        return "Bearer IGT:2:" + encoded

    def update_from_response_headers(self, headers: Mapping[str, Any]) -> None:
        """根据服务端响应头动态更新会话鉴权令牌、Claim 与区域提示。"""
        if not headers:
            return
        lower = {str(k).lower(): str(v) for k, v in headers.items() if v is not None}
        auth_val = lower.get("ig-set-authorization", "").strip()
        if auth_val:
            if auth_val.startswith("Bearer IGT:2:"):
                stripped = auth_val[len("Bearer IGT:2:"):].strip()
                if stripped:
                    self.auth_bearer = stripped
            elif not auth_val.endswith(":"):
                self.auth_bearer = auth_val
        claim = lower.get("x-ig-set-www-claim", "").strip()
        if claim:
            self.claims = claim
        rur_val = lower.get("ig-set-ig-u-rur", "").strip()
        if rur_val:
            self.rur = rur_val
        ds_uid = lower.get("ig-set-ig-u-ds-user-id", "").strip()
        if ds_uid.isdigit() and int(ds_uid) > 0:
            self.auth_ds_user_id = ds_uid
        region = lower.get("ig-set-ig-u-ig-direct-region-hint", "").strip()
        if region:
            self.region_hint = region
        key_id = lower.get("ig-set-password-encryption-key-id", "").strip()
        if key_id:
            self.enc_key_id = key_id
        pub_key = lower.get("ig-set-password-encryption-pub-key", "").strip()
        if pub_key:
            self.enc_pub_key = pub_key

    def get_bandwidth_data(self, ts_ms: int) -> Dict[str, str]:
        """生成并按时间窗缓存客户端带宽遥测指标。"""
        if not self.is_logged_in:
            return {"SpeedKbps": "-1.000", "TotalBytes": "0", "TotalTimeMs": "0"}
        if self._bandwidth_cache is None or (ts_ms - self._last_request_ts_ms) > self._bandwidth_window_ms:
            self._bandwidth_window_ms = random.randrange(30000, 150000)
            speed = random.randrange(2000, 5000)
            time_ms = random.randrange(500, 2500)
            self._bandwidth_cache = {
                "SpeedKbps": f"{speed}.000",
                "TotalBytes": str(speed * time_ms),
                "TotalTimeMs": str(time_ms),
            }
            self._last_request_ts_ms = ts_ms
        return self._bandwidth_cache

    def reset_transient_ids(self) -> None:
        """重置瞬态会话标识与短期鉴权缓存。"""
        self.pigeon_session_id = str(uuid.uuid4())
        self.attribution_id = str(uuid.uuid4())
        self.client_session_id = str(uuid.uuid4())
        self.claims = "0"
        self.seq_id = 0
        self.auth_bearer = ""

    def suspend_operation(self, operation: str, *, web: bool = False) -> None:
        """按操作粒度执行指数退避冷却（首次随机 15-60 秒，连续报错翻倍，上限 1 小时）。"""
        if not operation or operation == "Login":
            return
        bucket = self.web_suspended_ops if web else self.suspended_ops
        existing = bucket.get(operation)
        if existing is not None:
            existing.suspend_more()
        else:
            sec = random.randrange(15, 60)
            bucket[operation] = SuspendedOp(
                suspended_until=time.time() + sec,
                last_seconds=sec,
                consecutive_errors=1,
            )

    def is_operation_suspended(self, operation: str, *, web: bool = False) -> bool:
        if not operation or operation == "Login":
            return False
        bucket = self.web_suspended_ops if web else self.suspended_ops
        entry = bucket.get(operation)
        if entry is None:
            return False
        if time.time() >= entry.suspended_until:
            bucket.pop(operation, None)
            return False
        return True


def build_mobile_headers(
    session: InstagramSessionState,
    url: str,
    *,
    method: str = "GET",
    extra: Optional[Mapping[str, str]] = None,
    is_external: bool = False,
    timestamp_ms: Optional[int] = None,
) -> Dict[str, str]:
    """构造 Instagram Android Mobile API 标准请求头集合。"""
    hdrs: Dict[str, str] = {}
    if extra:
        hdrs.update({str(k): str(v) for k, v in extra.items() if v is not None})
    if is_external:
        return dict(sorted(hdrs.items(), key=lambda kv: kv[0]))

    app_locale = str(session.locale or "en_SG").replace("-", "_")
    lang_tag = app_locale.replace("_", "-")
    hdrs.setdefault("Accept-Language", lang_tag)
    hdrs.setdefault("Accept-Encoding", "gzip,deflate")
    hdrs.setdefault("User-Agent", session.mobile_user_agent())
    hdrs.setdefault("X-Fb-Http-Engine", "Liger")
    hdrs.setdefault("X-Fb-Server-Cluster", "True")
    hdrs.setdefault("X-Ig-App-Id", MOBILE_APP_ID)
    hdrs.setdefault("X-Ig-Capabilities", X_IG_CAPABILITIES)
    hdrs.setdefault("X-Fb-Client-Ip", "True")
    hdrs.setdefault("X-Ig-Connection-Type", "WIFI")
    hdrs.setdefault("X-Fb-Connection-Type", "WIFI")

    auth = session.authorization_cookie()
    if auth:
        hdrs.setdefault("Authorization", auth)

    if "api" in url:
        ts_ms = int(timestamp_ms if timestamp_ms and timestamp_ms > 0 else round(time.time() * 1000))
        bw = session.get_bandwidth_data(ts_ms)
        hdrs.setdefault("X-Ig-Device-Id", session.unique_id_ex)
        hdrs.setdefault("X-Ig-Family-Device-Id", session.family_device_guid)
        if session.auth_mid:
            hdrs.setdefault("X-Ig-App-Startup-Country", session.country_iso_code or "SG")
        hdrs.setdefault("X-Ig-Android-Id", session.device_id)
        hdrs.setdefault("X-Ig-App-Locale", app_locale)
        hdrs.setdefault("X-Ig-Device-Locale", app_locale)
        hdrs.setdefault("X-Ig-Mapped-Locale", app_locale)
        if session.nav_chain and not session.ignore_nav_chain:
            hdrs.setdefault("X-Ig-Nav-Chain", session.nav_chain)
        hdrs.setdefault("X-Ig-Timezone-Offset", str(session.timezone_offset))
        hdrs.setdefault("X-Ig-Www-Claim", session.claims or "0")
        hdrs.setdefault("X-Pigeon-Session-Id", f"UFS-{session.pigeon_session_id}-0")
        hdrs.setdefault("X-Pigeon-Rawclienttime", format_raw_client_time(ts_ms))
        hdrs.setdefault("X-Ig-Bandwidth-Speed-Kbps", bw["SpeedKbps"])
        hdrs.setdefault("X-Ig-Bandwidth-TotalTime-Ms", bw["TotalTimeMs"])
        hdrs.setdefault("X-Ig-Bandwidth-TotalBytes-B", bw["TotalBytes"])
        hdrs.setdefault("X-Bloks-Version-Id", BLOKS_VERSION_ID)
        hdrs.setdefault("X-Bloks-Is-Layout-Rtl", "false")
        if method.upper() == "GET":
            hdrs.setdefault("Accept", "*/*")

    if session.auth_mid:
        hdrs.setdefault("X-Mid", session.auth_mid)
    hdrs.setdefault("Ig-Intended-User-Id", session.auth_ds_user_id or "0")
    if session.region_hint and "Ig-U-Ig-Direct-Region-Hint" not in hdrs:
        hdrs["Ig-U-Ig-Direct-Region-Hint"] = session.region_hint
    if session.rur and "Ig-U-Rur" not in hdrs:
        hdrs["Ig-U-Rur"] = session.rur
    if session.auth_ds_user_id and "Ig-U-Ds-User-Id" not in hdrs:
        hdrs["Ig-U-Ds-User-Id"] = session.auth_ds_user_id
    if session.shb_id and "Ig-U-Shbid" not in hdrs:
        hdrs["Ig-U-Shbid"] = session.shb_id
    if session.shb_ts and "Ig-U-Shbts" not in hdrs:
        hdrs["Ig-U-Shbts"] = session.shb_ts
    if "Priority" not in hdrs:
        hdrs["Priority"] = "u=" + get_priority(url)

    return dict(sorted(hdrs.items(), key=lambda kv: kv[0]))


def build_web_headers(
    session: InstagramSessionState,
    url: str,
    *,
    referer: str = "https://www.instagram.com/",
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """构造 Instagram Web 会话 HTTP 请求头（使用浏览器 Cookie + `X-CSRFToken` + `X-Ig-App-Id`，不混入移动端 Bearer）。"""
    lang_tag = str(session.locale or "en_SG").replace("_", "-")
    base_lang = lang_tag.split("-", 1)[0].lower() or "en"
    accept_lang = (
        f"{lang_tag},{base_lang};q=0.9,en;q=0.8"
        if base_lang != "en"
        else f"{lang_tag},en;q=0.9"
    )
    hdrs: Dict[str, str] = {
        "Accept": "*/*",
        "Accept-Language": accept_lang,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site" if "i.instagram.com" in url else "same-origin",
        "X-Ig-App-Id": WEB_APP_ID,
        "X-Instagram-Ajax": WEB_AJAX_ID,
        "X-Asbd-Id": WEB_ASBD_ID,
        "X-Requested-With": "XMLHttpRequest",
    }
    if session.web_user_agent:
        ua = session.web_user_agent
        hdrs["User-Agent"] = ua
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
        hdrs["sec-ch-ua"] = brands
        hdrs["sec-ch-ua-mobile"] = "?0"
        hdrs["sec-ch-ua-platform"] = platform
    if referer:
        hdrs["Referer"] = referer
    if "i.instagram.com" in url:
        hdrs["Origin"] = WWW_BASE
    if session.browser_cookies:
        hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in session.browser_cookies.items() if v != "")
    if session.csrf_token:
        hdrs["X-CSRFToken"] = session.csrf_token
    if session.claims:
        hdrs["X-IG-WWW-Claim"] = session.claims
    if extra:
        hdrs.update({str(k): str(v) for k, v in extra.items() if v is not None})
    return hdrs
