"""标签规则（任务 B）。PRD 7.2：关键词初筛 + 抽取三值 → 每一维结论（是/否/待核），总标签与分组。"""
from __future__ import annotations

import re

from .models import LabelDim

AI_KEYWORDS: tuple[str, ...] = (
    "AI", "agent", "agentic", "LLM", "model", "machine learning", "generative",
    "world model", "computer vision", "voice AI",
)
SOCIAL_KEYWORDS: tuple[str, ...] = (
    "social", "community", "friends", "dating", "group", "creator", "forum", "events",
)
_PATTERNS = {
    dim: re.compile(r"\b(?:" + "|".join(
        (r"communit(?:y|ies)" if word == "community" else
         re.escape(word).replace(r"\ ", r"\s+") + ("s?" if word in {"agent", "LLM", "model", "world model", "group", "creator", "forum"} else ""))
        for word in words
    ) + r")\b", re.IGNORECASE)
    for dim, words in (("ai", AI_KEYWORDS), ("social", SOCIAL_KEYWORDS))
}


def keyword_hit(text: str, dim: str) -> bool:
    """dim = "ai" | "social"；按词边界、不区分大小写。"""
    if dim not in _PATTERNS:
        raise ValueError(f"未知标签维度：{dim}")
    return bool(_PATTERNS[dim].search(text))


def decide_dim(keyword_hit: bool, dim: LabelDim) -> str:
    """7.2 判定表 → "是" | "否" | "待核"。"""
    quote = bool(dim.quote and dim.quote.strip())
    if dim.verdict == "是":
        return "是" if quote else "待核"
    if dim.verdict == "否":
        return "否" if not keyword_hit or quote else "待核"
    return "待核"


def overall(ai: str, social: str) -> tuple[str, str]:
    """返回 (tag_label, group)。group ∈ {"AI+社交","社交","AI","都不是","标签待核"}；
    tag_label 例 "AI相关 + 社交/社群相关"、"都不是"、"AI相关 + 标签待核"、"标签待核"。"""
    if ai not in {"是", "否", "待核"} or social not in {"是", "否", "待核"}:
        raise ValueError("每一维必须是 是、否、待核")
    parts = []
    if ai == "是":
        parts.append("AI相关")
    if social == "是":
        parts.append("社交/社群相关")
    if "待核" in (ai, social):
        return " + ".join(parts + ["标签待核"]), "标签待核"
    if ai == social == "是":
        return " + ".join(parts), "AI+社交"
    if parts:
        return parts[0], "AI" if ai == "是" else "社交"
    return "都不是", "都不是"
