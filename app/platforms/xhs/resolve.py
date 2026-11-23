"""从分享链接/主页 URL 解析小红书 user_id / note_id(及 xsec_token)。
职责与 app/douyin/resolve.py 一致,但小红书很多接口需要 URL 里带 xsec_token,
所以这里尽量把 xsec_token / xsec_source 一并解析出来。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from curl_cffi.requests import AsyncSession

from ...browser.manager import normalize_proxy
from ...netfp import impersonate_for_ua

_ID_RE = r"[0-9a-fA-F]{24}"
_NOTE_PATH_RE = re.compile(rf"/(?:explore|discovery/item)/({_ID_RE})")
_USER_PATH_RE = re.compile(rf"/user/profile/({_ID_RE})")
_XSEC_TOKEN_RE = re.compile(r"xsec_token=([A-Za-z0-9_\-=+%]+)")
_XSEC_SOURCE_RE = re.compile(r"xsec_source=([A-Za-z0-9_\-]+)")
_SHORT_RE = re.compile(r"https?://xhslink\.com/[\w/\-]+")
_BARE_ID_RE = re.compile(rf"^{_ID_RE}$")


@dataclass
class NoteRef:
    note_id: str
    xsec_token: str = ""
    xsec_source: str = ""


@dataclass
class UserRef:
    user_id: str
    xsec_token: str = ""
    xsec_source: str = ""


def _tok(text: str) -> tuple[str, str]:
    """从一段 URL 文本里取 xsec_token / xsec_source(URL 解码 %3D 等)。"""
    import urllib.parse as up
    t = _XSEC_TOKEN_RE.search(text)
    s = _XSEC_SOURCE_RE.search(text)
    return (up.unquote(t.group(1)) if t else "",
            up.unquote(s.group(1)) if s else "")


async def _follow_short(text: str, user_agent: str, proxy: str = "") -> Optional[str]:
    """跟随 xhslink.com 短链拿到最终 xiaohongshu.com URL。"""
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


async def resolve_note(text: str, user_agent: str, proxy: str = "") -> Optional[NoteRef]:
    """支持:explore/discovery 链接、xhslink 短链、纯 24 位 note_id。"""
    text = text.strip()
    if _BARE_ID_RE.match(text):
        return NoteRef(note_id=text)
    m = _NOTE_PATH_RE.search(text)
    if m:
        tok, src = _tok(text)
        return NoteRef(note_id=m.group(1), xsec_token=tok, xsec_source=src or "pc_feed")
    final = await _follow_short(text, user_agent, proxy=proxy)
    if final:
        m = _NOTE_PATH_RE.search(final)
        if m:
            tok, src = _tok(final)
            return NoteRef(note_id=m.group(1), xsec_token=tok, xsec_source=src or "pc_feed")
    return None


async def resolve_user(text: str, user_agent: str, proxy: str = "") -> Optional[UserRef]:
    """支持:user/profile 链接、xhslink 短链、纯 24 位 user_id。"""
    text = text.strip()
    if _BARE_ID_RE.match(text):
        return UserRef(user_id=text)
    m = _USER_PATH_RE.search(text)
    if m:
        tok, src = _tok(text)
        return UserRef(user_id=m.group(1), xsec_token=tok, xsec_source=src or "pc_search")
    final = await _follow_short(text, user_agent, proxy=proxy)
    if final:
        m = _USER_PATH_RE.search(final)
        if m:
            tok, src = _tok(final)
            return UserRef(user_id=m.group(1), xsec_token=tok, xsec_source=src or "pc_search")
    return None


def looks_like_note(text: str) -> bool:
    """判断输入更像「笔记链接」还是「创作者主页」。"""
    return bool(_NOTE_PATH_RE.search(text)) and not _USER_PATH_RE.search(text)
