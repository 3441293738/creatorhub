"""视频号 id 解析。对标 kuaishou/resolve.py,但视频号无对外公开主页,能力有限。

视频号标识:
  - finderUsername:形如 `v2_060000...@finder` 或 `sph...`(账号级,本账号自己一般不需要)
  - 单条作品 objectId / exportId:纯数字长串(本账号作品列表里带)
分享链接形态多为 `https://channels.weixin.qq.com/...` 或小程序卡片,对外解析不稳定。
故这里只做「尽力从文本里抠出 finder id / objectId」,拿不到就返回 None,由调用方降级
到「本账号」语义(视频号主要就是操作本账号)。
"""
from __future__ import annotations

import re
from typing import Optional

# finderUsername:v2_ 开头的十六进制串 + @finder,或 sph 开头
_FINDER_RE = re.compile(r"(v2_[0-9a-fA-F]{20,}@?f?i?n?d?e?r?|sph[0-9A-Za-z]{6,})")
# 视频号作品 objectId / exportId:较长的纯数字串
_OBJECT_RE = re.compile(r"\b(\d{16,})\b")
_BARE_ID_RE = re.compile(r"^[0-9A-Za-z_@]{8,}$")


async def resolve_channels_user_id(text: str, user_agent: str = "") -> Optional[str]:
    """从文本/链接里尽力解析 finderUsername。视频号一般操作本账号,拿不到返回 None。"""
    text = (text or "").strip()
    m = _FINDER_RE.search(text)
    if m:
        return m.group(1)
    if "/" not in text and _BARE_ID_RE.match(text):
        return text
    return None


async def resolve_channels_photo_id(text: str, user_agent: str = "") -> Optional[str]:
    """从文本/链接里尽力解析单条作品 objectId / exportId。"""
    text = (text or "").strip()
    m = _OBJECT_RE.search(text)
    if m:
        return m.group(1)
    if "/" not in text and text.isdigit():
        return text
    return None


def looks_like_photo(text: str) -> bool:
    """判断输入更像「单条作品」(长数字 objectId)还是「账号」(finderUsername)。"""
    return bool(_OBJECT_RE.search(text)) and not _FINDER_RE.search(text)
