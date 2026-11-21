"""X (Twitter) 推文加权字数统计、实体识别与智能截断器。

遵循官方 `twitter-text` v3 加权计数规范：
- 标准推文上限 280 权重，X Premium 长推文上限 4000 权重，HTTP(S) 链接经 `t.co` 固定计为 23 权重；
- 实体提取严格排除 URL 区间内部的 `@` / `#` 字符，避免误切分链接片段；
- 截断前预留省略号 `…` 自身权重预算，确保输出严格不超过上限，并按 Unicode 码点迭代避免拆裂 Emoji。
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

DEFAULT_TWEET_LIMIT = 280
PREMIUM_TWEET_LIMIT = 4000
TCO_URL_WEIGHT = 23

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{1,15})\b")
_HASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])#([\w\u4e00-\u9fff]{1,100})")
_TRAILING_URL_PUNCT = ".,;:!?)】》'\""
_BOUNDARY_CHARS = frozenset(" \t\n\r,.;:!?，。！？；：、）】》-—/")


def char_weight(ch: str) -> int:
    """按 Twitter `twitter-text` v3 权重规则近似计算单码点权重（ASCII/拉丁=1，CJK/全角/Emoji=2）。"""
    if not ch:
        return 0
    cp = ord(ch)
    # twitter-text v3 default weight=2 ranges: U+0000..U+10FF is weight 1 (plus general punct exceptions)
    if cp <= 0x10FF:
        return 1
    # U+2000..U+200D (zero-width joiners/spaces) -> 1; U+2010..U+201F / U+2026 ('…') -> 2
    if 0x2000 <= cp <= 0x200D:
        return 1
    return 2


def _url_spans(text: str) -> List[Tuple[int, int, str]]:
    """提取非重叠的 HTTP(S) URL 区间 `(start, end, url)`，剥离句末粘连标点。"""
    spans: List[Tuple[int, int, str]] = []
    for m in _URL_RE.finditer(text or ""):
        start, end = m.span()
        raw = m.group(0)
        trimmed = raw.rstrip(_TRAILING_URL_PUNCT)
        if not trimmed or trimmed in {"http://", "https://"}:
            continue
        end = start + len(trimmed)
        spans.append((start, end, trimmed))
    return spans


def twitter_weighted_length(text: str, *, url_weight: int = TCO_URL_WEIGHT) -> int:
    """计算推文加权长度：每条 HTTP(S) 链接固定计为 23，其余字符按码点权重累加。"""
    s = str(text or "")
    if not s:
        return 0
    spans = _url_spans(s)
    if not spans:
        return sum(char_weight(ch) for ch in s)

    total = 0
    cursor = 0
    for start, end, _url in spans:
        if start > cursor:
            total += sum(char_weight(ch) for ch in s[cursor:start])
        total += url_weight
        cursor = end
    if cursor < len(s):
        total += sum(char_weight(ch) for ch in s[cursor:])
    return total


def is_within_twitter_limit(text: str, *, limit: int = DEFAULT_TWEET_LIMIT) -> bool:
    return twitter_weighted_length(text) <= max(1, int(limit))


def extract_tweet_entities(text: str) -> Dict[str, List[str]]:
    """提取推文中的 URL、`@mention` 与 `#hashtag`，严格排除 URL 内部的 `@`/`#`。"""
    s = str(text or "")
    spans = _url_spans(s)

    def _inside_url(pos: int) -> bool:
        return any(start <= pos < end for start, end, _ in spans)

    urls = [u for _, _, u in spans]
    mentions = [
        m.group(1)
        for m in _MENTION_RE.finditer(s)
        if not _inside_url(m.start())
    ]
    hashtags = [
        m.group(1)
        for m in _HASHTAG_RE.finditer(s)
        if not _inside_url(m.start())
    ]
    return {"urls": urls, "mentions": mentions, "hashtags": hashtags}


def _truncate_plain_weighted(text: str, budget: int, ellipsis: str = "…") -> str:
    """在 `budget` 加权上限内安全截断文本（含 `ellipsis` 权重），优先落在词或标点边界。"""
    s = str(text or "").strip()
    if not s or budget <= 0:
        return ""
    if twitter_weighted_length(s) <= budget:
        return s

    ell_w = sum(char_weight(ch) for ch in ellipsis)
    target = max(0, budget - ell_w)
    if target <= 0:
        return ellipsis if ell_w <= budget else ""

    spans = _url_spans(s)
    span_map = {start: (end, url) for start, end, url in spans}

    used = 0
    idx = 0
    last_boundary_idx = 0
    n = len(s)
    while idx < n:
        if idx in span_map:
            end, _url = span_map[idx]
            if used + TCO_URL_WEIGHT > target:
                break
            used += TCO_URL_WEIGHT
            idx = end
            last_boundary_idx = idx
            continue
        ch = s[idx]
        w = char_weight(ch)
        if used + w > target:
            break
        used += w
        idx += 1
        if ch in _BOUNDARY_CHARS:
            last_boundary_idx = idx

    # 若词边界回退不超过 25% 已用长度，则优先使用自然词/标点边界
    cut = idx
    if last_boundary_idx > 0 and (idx - last_boundary_idx) <= max(12, idx // 4):
        cut = last_boundary_idx

    head = s[:cut].rstrip(" \t\n\r,.;:!?，。！？；：、-")
    if not head:
        head = s[:idx].rstrip()
    return f"{head}{ellipsis}" if head else ""


def trim_to_twitter_limit(
    text: str,
    limit: int = DEFAULT_TWEET_LIMIT,
    *,
    preserve_last_url: bool = True,
    ellipsis: str = "…",
) -> str:
    """将推文智能裁剪至加权字符上限内（默认 280，X Premium 可传 4000）。

    当 `preserve_last_url=True` 且末尾带有外链时，
    自动保留末尾完整 URL 并仅裁剪正文前缀，防止把外链截断成无效链接。
    """
    s = str(text or "").strip()
    cap = max(10, int(limit or DEFAULT_TWEET_LIMIT))
    if twitter_weighted_length(s) <= cap:
        return s

    if preserve_last_url:
        spans = _url_spans(s)
        if spans:
            last_start, last_end, last_url = spans[-1]
            tail_after = s[last_end:].strip()
            # 末尾即外链（或外链后仅有少量标签）且预算足够容纳外链时，保留该外链
            if not tail_after and cap > (TCO_URL_WEIGHT + 6):
                prefix = s[:last_start].rstrip()
                prefix_budget = cap - TCO_URL_WEIGHT - 1  # 1 weight for separating space
                trimmed_prefix = _truncate_plain_weighted(prefix, prefix_budget, ellipsis=ellipsis)
                if trimmed_prefix:
                    candidate = f"{trimmed_prefix} {last_url}"
                    if twitter_weighted_length(candidate) <= cap:
                        return candidate
                if TCO_URL_WEIGHT <= cap:
                    return last_url

    return _truncate_plain_weighted(s, cap, ellipsis=ellipsis)


def build_tweet_caption(
    title: str,
    desc: str,
    topics: str = "",
    *,
    limit: int = DEFAULT_TWEET_LIMIT,
    long_text: bool = False,
    preserve_last_url: bool = True,
) -> str:
    """合并标题、正文与去重 `#话题`，并按标准 280 或长推文 4000 加权上限安全收敛。"""
    effective_limit = PREMIUM_TWEET_LIMIT if long_text else max(10, int(limit or DEFAULT_TWEET_LIMIT))
    parts: List[str] = []
    t = (title or "").strip()
    d = (desc or "").strip()
    if t:
        parts.append(t)
    if d and d != t:
        parts.append(d)

    existing_folded = "\n".join(parts).casefold()
    tags: List[str] = []
    seen: set[str] = set()
    for raw in (topics or "").replace("，", ",").split(","):
        tag = raw.strip().lstrip("#").strip()
        if not tag:
            continue
        # X hashtag 不含空格
        tag = re.sub(r"\s+", "_", tag)
        key = tag.casefold()
        if key in seen or f"#{key}" in existing_folded:
            continue
        seen.add(key)
        tags.append(f"#{tag}")

    body = "\n".join(parts).strip()
    if tags:
        tag_line = " ".join(tags)
        combined = f"{body}\n{tag_line}".strip() if body else tag_line
    else:
        combined = body

    return trim_to_twitter_limit(
        combined,
        limit=effective_limit,
        preserve_last_url=preserve_last_url,
    )
