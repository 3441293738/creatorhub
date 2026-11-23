"""从分享链接/主页 URL 解析快手 user_id / photo_id,与 douyin/resolve.py 保持统一职责边界。

快手 PC 链接形态:
  - 主页:  https://www.kuaishou.com/profile/3x...           (user_id)
  - 作品:  https://www.kuaishou.com/short-video/3x...        (photo_id)
  - 短链:  https://v.kuaishou.com/xxxxx                      (跟随跳转)
快手 user_id / photo_id 都是 [0-9a-zA-Z]+ 串(常以 3x 开头),无法仅凭字符区分,
故纯 id 输入时由调用方按场景(创作者监控 / 单条作品)决定当作哪种用。
"""
from __future__ import annotations

import re
from typing import Optional

from curl_cffi.requests import AsyncSession

from ...browser.manager import normalize_proxy
from ...netfp import impersonate_for_ua

_PROFILE_RE = re.compile(r"/profile/([0-9a-zA-Z_\-]+)")
_PHOTO_RE = re.compile(r"/(?:short-video|video|photo)/([0-9a-zA-Z_\-]+)")
_SHORT_RE = re.compile(r"https?://v\.kuaishou\.com/[\w\-]+")
_BARE_ID_RE = re.compile(r"^[0-9a-zA-Z_\-]{8,}$")


async def _follow_short(text: str, user_agent: str, proxy: str = "") -> Optional[str]:
    short = _SHORT_RE.search(text)
    if not short:
        return None
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
            return str(r.url)
    except Exception:
        return None


async def resolve_ks_user_id(text: str, user_agent: str, proxy: str = "") -> Optional[str]:
    """支持:主页链接 /profile/xxx、v.kuaishou.com 短链、纯 user_id。"""
    text = (text or "").strip()
    m = _PROFILE_RE.search(text)
    if m:
        return m.group(1)
    final = await _follow_short(text, user_agent, proxy=proxy)
    if final:
        m = _PROFILE_RE.search(final)
        if m:
            return m.group(1)
    # 纯 id(且不含路径分隔符)
    if "/" not in text and _BARE_ID_RE.match(text):
        return text
    return None


async def resolve_ks_photo_id(text: str, user_agent: str, proxy: str = "") -> Optional[str]:
    """从作品链接/短链/纯 id 解析 photo_id。"""
    text = (text or "").strip()
    m = _PHOTO_RE.search(text)
    if m:
        return m.group(1)
    final = await _follow_short(text, user_agent, proxy=proxy)
    if final:
        m = _PHOTO_RE.search(final)
        if m:
            return m.group(1)
    if "/" not in text and _BARE_ID_RE.match(text):
        return text
    return None


def looks_like_photo(text: str) -> bool:
    """判断输入更像「作品链接」还是「创作者主页」。"""
    return bool(_PHOTO_RE.search(text)) and not _PROFILE_RE.search(text)
