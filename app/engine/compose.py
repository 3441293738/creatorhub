"""自动评论文案合成。

当前实现:从模板库随机取一条 -> 展开 spintax {a|b|c} -> 填充 {nick}/{kw} 变量。
目的不是写得多花哨,而是**让每条评论都不一样**:平台对同质化评论判垃圾营销极敏感,
同一串文案反复发会被「仅自己可见」(shadow ban)甚至禁言。

预留 `generate(ctx)` 钩子:以后接入模型 API(读对方评论/笔记内容生成自然回复)时,
把 render() 换成 generate() 即可,引擎调用处不变。
"""
from __future__ import annotations

import json
import random
import re
from typing import List, Optional

# {a|b|c} 形式的同义替换(spintax),不支持嵌套
_SPINTAX = re.compile(r"\{([^{}|]+(?:\|[^{}|]+)+)\}")


def parse_templates(templates_json: str) -> List[str]:
    """规则里存的是 JSON 字符串数组;也容忍换行分隔的纯文本。"""
    s = (templates_json or "").strip()
    if not s:
        return []
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return [str(t).strip() for t in v if str(t).strip()]
    except Exception:
        pass
    return [line.strip() for line in s.splitlines() if line.strip()]


def _expand_spintax(text: str, rnd: random.Random) -> str:
    # 反复替换直到没有 {a|b|c}(每次替换一处,支持一行多个)
    for _ in range(20):
        m = _SPINTAX.search(text)
        if not m:
            break
        choice = rnd.choice(m.group(1).split("|"))
        text = text[:m.start()] + choice + text[m.end():]
    return text


def _fill_vars(text: str, ctx: dict) -> str:
    nick = (ctx.get("nick") or "").strip()
    kw = (ctx.get("kw") or "").strip()
    # @对方:仅当模板显式写了 {nick} 才带上,避免无脑 @ 人
    text = text.replace("{nick}", nick)
    text = text.replace("{kw}", kw)
    # 收尾:清掉空 @ 和多余空格
    text = re.sub(r"@(\s|$)", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def render(templates: List[str], ctx: Optional[dict] = None,
           seed: Optional[str] = None) -> str:
    """从模板库合成一条评论文案。templates 为空时返回空串(调用方应跳过)。
    seed:给定则结果可复现(同一目标不会每轮抖出不同文案,利于去重测试)。"""
    if not templates:
        return ""
    ctx = ctx or {}
    rnd = random.Random(seed) if seed else random
    text = _expand_spintax(rnd.choice(templates), rnd)
    return _fill_vars(text, ctx)[:200]   # 评论长度兜底


# ── 大模型 API 文案生成(OpenAI 兼容 /chat/completions)──
# 绝大多数第三方大模型(DeepSeek / 通义千问 / 月之暗面 / 智谱 / OpenAI 等)都提供
# OpenAI 兼容接口,这里用统一的 chat/completions 调用,换 base_url + model 即可接入。
# 失败/未配置时由引擎回退到 render(),所以这条路永远是"锦上添花"而非必需。
_DEFAULT_AI_PROMPT = (
    "你是社媒运营助手,请写一条简短自然、口语化的中文{kind_label}。\n"
    "要求:不超过 30 字;不要书名号/引号;不要解释;直接输出{kind_label}本身。\n"
    "平台:{platform}\n对方内容:{source_text}\n对方昵称:{nick}\n关键词:{kw}"
)


async def generate(ctx: dict, ai: dict) -> str:
    """调用大模型 API 生成一条文案。ai 配置不全或请求失败时抛错(引擎会回退模板)。
    ai: {base_url, api_key, model, prompt, temperature, timeout}"""
    import httpx
    base = (ai.get("base_url") or "").rstrip("/")
    key = ai.get("api_key") or ""
    model = ai.get("model") or ""
    if not (base and key and model):
        raise RuntimeError("AI 配置不完整(需 base_url / api_key / model)")
    vals = {
        "source_text": (ctx.get("source_text") or "").strip()[:400],
        "nick": ctx.get("nick", "") or "", "kw": ctx.get("kw", "") or "",
        "platform": ctx.get("platform", "") or "",
        "kind_label": "回复" if ctx.get("mode") == "auto_reply" else "评论",
    }
    tmpl = ai.get("prompt") or _DEFAULT_AI_PROMPT
    try:
        user_msg = tmpl.format(**vals)
    except Exception:
        user_msg = _DEFAULT_AI_PROMPT.format(**vals)
    body = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "你只输出一条评论文本本身,简短自然口语化,不要任何解释、前缀或引号。"},
            {"role": "user", "content": user_msg},
        ],
        "temperature": float(ai.get("temperature") or 0.9),
        "max_tokens": 120,
    }
    timeout = float(ai.get("timeout") or 20)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as cli:
        r = await cli.post(base + "/chat/completions", json=body,
                           headers={"Authorization": f"Bearer {key}",
                                    "Content-Type": "application/json"})
    r.raise_for_status()
    j = r.json()
    text = (((j.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    text = text.strip().strip('"“”\'').strip()
    if not text:
        raise RuntimeError("AI 返回空文案")
    return text[:200]
