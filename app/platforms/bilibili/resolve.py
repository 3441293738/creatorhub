"""哔哩哔哩分享链接、BV/av 号、动态 opus 与 UP 主 UID 解析。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from curl_cffi.requests import AsyncSession

from ...browser.manager import normalize_proxy
from ...netfp import impersonate_for_ua

# 2024+ Bilibili 官方新版 BV <-> AID 转换常量
_XOR_CODE = 23442827791579
_MASK_CODE = 2251799813685247
_MAX_AID = 1 << 51
_BASE = 58
_BV_ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
_BV_CHAR_MAP = {ch: idx for idx, ch in enumerate(_BV_ALPHABET)}

_BV_RE = re.compile(r"\b(BV1[0-9A-Za-z]{9})\b")
_AV_RE = re.compile(r"(?:^|/|(?<![0-9A-Za-z]))av(\d{1,20})\b", re.IGNORECASE)
_SPACE_RE = re.compile(r"(?:space\.bilibili\.com|m\.bilibili\.com/space)/(\d+)", re.IGNORECASE)
_VIDEO_PATH_RE = re.compile(r"bilibili\.com/video/(BV1[0-9A-Za-z]{9}|av\d+)", re.IGNORECASE)
_OPUS_RE = re.compile(r"(?:bilibili\.com/opus|t\.bilibili\.com)/(\d{10,24})", re.IGNORECASE)
_SHORT_RE = re.compile(r"https?://(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z_\-]+", re.IGNORECASE)
_UID_PREFIX_RE = re.compile(r"^(?:uid\s*[:：]?\s*)?(\d{1,20})$", re.IGNORECASE)


@dataclass(frozen=True)
class BilibiliWorkRef:
    work_id: str          # BV1... 或 opus 数字 ID
    bvid: str = ""
    aid: int = 0
    opus_id: str = ""
    source_url: str = ""


def av_to_bv(aid: int) -> str:
    """将正整数 av 号离线转换为标准 12 位 BV 号。"""
    aid_val = int(aid)
    if aid_val <= 0:
        raise ValueError(f"invalid aid: {aid}")
    chars = ["B", "V", "1", "0", "0", "0", "0", "0", "0", "0", "0", "0"]
    bv_idx = 11
    tmp = (_MAX_AID | aid_val) ^ _XOR_CODE
    while tmp > 0 and bv_idx >= 3:
        chars[bv_idx] = _BV_ALPHABET[tmp % _BASE]
        tmp //= _BASE
        bv_idx -= 1
    chars[3], chars[9] = chars[9], chars[3]
    chars[4], chars[7] = chars[7], chars[4]
    return "".join(chars)


def bv_to_av(bvid: str) -> int:
    """将标准 12 位 BV 号离线转换为正整数 av 号。"""
    s = str(bvid or "").strip()
    if len(s) != 12 or not s.startswith("BV1"):
        raise ValueError(f"invalid bvid: {bvid}")
    chars = list(s)
    chars[3], chars[9] = chars[9], chars[3]
    chars[4], chars[7] = chars[7], chars[4]
    tmp = 0
    for ch in chars[3:]:
        if ch not in _BV_CHAR_MAP:
            raise ValueError(f"invalid bvid character in {bvid}")
        tmp = tmp * _BASE + _BV_CHAR_MAP[ch]
    return (tmp & _MASK_CODE) ^ _XOR_CODE


_DEFAULT_RESOLVE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


async def _follow_b23(text: str, user_agent: str, *, proxy: str = "") -> Optional[str]:
    short = _SHORT_RE.search(text or "")
    if not short:
        return None
    ua = str(user_agent or "").strip() or _DEFAULT_RESOLVE_UA
    from ...netfp import curl_proxy_url
    clean_proxy = curl_proxy_url(proxy) or None
    try:
        async with AsyncSession(impersonate=impersonate_for_ua(ua), trust_env=False) as cli:
            r = await cli.get(
                short.group(0),
                headers={"User-Agent": ua},
                proxy=clean_proxy,
                timeout=15,
                allow_redirects=True,
            )
            return str(r.url or "")
    except Exception:
        return None


def _extract_work_sync(text: str) -> Optional[BilibiliWorkRef]:
    raw = (text or "").strip()
    if not raw:
        return None
    m_bv = _BV_RE.search(raw)
    if m_bv:
        bvid = m_bv.group(1)
        try:
            aid = bv_to_av(bvid)
        except ValueError:
            aid = 0
        return BilibiliWorkRef(
            work_id=bvid,
            bvid=bvid,
            aid=aid,
            source_url=f"https://www.bilibili.com/video/{bvid}",
        )
    m_av = _AV_RE.search(raw)
    if m_av:
        aid = int(m_av.group(1))
        bvid = av_to_bv(aid)
        return BilibiliWorkRef(
            work_id=bvid,
            bvid=bvid,
            aid=aid,
            source_url=f"https://www.bilibili.com/video/{bvid}",
        )
    m_opus = _OPUS_RE.search(raw)
    if m_opus:
        opus_id = m_opus.group(1)
        return BilibiliWorkRef(
            work_id=opus_id,
            opus_id=opus_id,
            source_url=f"https://www.bilibili.com/opus/{opus_id}",
        )
    return None


async def resolve_bili_work_ref(
    text: str,
    user_agent: str = "",
    *,
    proxy: str = "",
) -> Optional[BilibiliWorkRef]:
    """从 BV 号、av 号、视频/动态链接或 b23.tv 短链解析作品引用。"""
    ref = _extract_work_sync(text)
    if ref is not None:
        return ref
    final_url = await _follow_b23(text, user_agent, proxy=proxy)
    if final_url:
        return _extract_work_sync(final_url)
    return None


async def resolve_bili_work(
    text: str,
    user_agent: str = "",
    *,
    proxy: str = "",
) -> Optional[str]:
    """解析并返回规范作品 ID（视频统一归一为 `BV1...`，图文动态返回 opus ID）。"""
    ref = await resolve_bili_work_ref(text, user_agent, proxy=proxy)
    return ref.work_id if ref else None


async def resolve_bili_mid(
    text: str,
    user_agent: str = "",
    *,
    proxy: str = "",
) -> Optional[str]:
    """从空间链接 `space.bilibili.com/<mid>`、`b23.tv` 短链或纯数字 UID 解析 UP 主 mid。"""
    raw = (text or "").strip()
    if not raw:
        return None
    m_space = _SPACE_RE.search(raw)
    if m_space:
        return m_space.group(1)
    if _SHORT_RE.search(raw):
        final_url = await _follow_b23(raw, user_agent, proxy=proxy)
        if final_url:
            m_space = _SPACE_RE.search(final_url)
            if m_space:
                return m_space.group(1)
    if "/" not in raw and not _BV_RE.search(raw) and not _AV_RE.search(raw):
        m_uid = _UID_PREFIX_RE.match(raw)
        if m_uid:
            return m_uid.group(1)
    return None


def looks_like_bili_work(text: str) -> bool:
    """判断输入更像单条视频/动态作品而非 UP 主主页。"""
    raw = (text or "").strip()
    if _SPACE_RE.search(raw):
        return False
    return bool(
        _BV_RE.search(raw)
        or _AV_RE.search(raw)
        or _VIDEO_PATH_RE.search(raw)
        or _OPUS_RE.search(raw)
    )
