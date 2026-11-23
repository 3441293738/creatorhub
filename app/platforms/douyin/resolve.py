"""从抖音分享链接或主页 URL 解析 `sec_uid` 与作品标识。"""
from __future__ import annotations

import re
from typing import Optional

from curl_cffi.requests import AsyncSession

from ...browser.manager import normalize_proxy
from ...netfp import impersonate_for_ua

_SEC_UID_RE = re.compile(r"sec_uid=([\w\-]+)")
_USER_PATH_RE = re.compile(r"/user/([\w\-]+)")
_SHORT_RE = re.compile(r"https?://v\.douyin\.com/[\w\-]+")
_AWEME_RE = re.compile(r"/(?:video|note|slides)/(\d+)")
_MODAL_RE = re.compile(r"modal_id=(\d+)")


async def resolve_sec_uid(text: str, user_agent: str, *, proxy: str = "") -> Optional[str]:
    """支持:直接 sec_uid、主页链接 douyin.com/user/xxx、短链 v.douyin.com/xxx。"""
    text = text.strip()

    # 已是 sec_uid(MS4w 开头的长串)
    if re.fullmatch(r"MS4w[\w\-]{20,}", text):
        return text

    m = _SEC_UID_RE.search(text) or _USER_PATH_RE.search(text)
    if m:
        return m.group(1)

    # 短链:跟随跳转拿到最终 URL
    short = _SHORT_RE.search(text)
    if short:
        from ...netfp import curl_proxy_url
        norm_proxy = curl_proxy_url(proxy) or None
        try:
            async with AsyncSession(
                impersonate=impersonate_for_ua(user_agent),
                proxy=norm_proxy,
                trust_env=False,
            ) as cli:
                r = await cli.get(short.group(0), headers={"User-Agent": user_agent},
                                  timeout=15, proxy=norm_proxy)
                final = str(r.url)
                m = _SEC_UID_RE.search(final) or _USER_PATH_RE.search(final)
                if m:
                    return m.group(1)
        except Exception:
            return None
    return None


async def resolve_aweme_id(text: str, user_agent: str, *, proxy: str = "") -> Optional[str]:
    """从视频链接/短链/纯数字 id 解析 aweme_id。"""
    text = text.strip()
    if re.fullmatch(r"\d{15,}", text):
        return text
    m = _AWEME_RE.search(text) or _MODAL_RE.search(text)
    if m:
        return m.group(1)
    short = _SHORT_RE.search(text)
    if short:
        from ...netfp import curl_proxy_url
        norm_proxy = curl_proxy_url(proxy) or None
        try:
            async with AsyncSession(
                impersonate=impersonate_for_ua(user_agent),
                proxy=norm_proxy,
                trust_env=False,
            ) as cli:
                r = await cli.get(short.group(0), headers={"User-Agent": user_agent},
                                  timeout=15, proxy=norm_proxy)
                final = str(r.url)
                m = _AWEME_RE.search(final) or _MODAL_RE.search(final)
                if m:
                    return m.group(1)
        except Exception:
            return None
    return None


def looks_like_video(text: str) -> bool:
    """判断输入更像"视频链接"还是"账号"。"""
    return bool(_AWEME_RE.search(text) or _MODAL_RE.search(text)
                or re.fullmatch(r"\d{15,}", text.strip()))
