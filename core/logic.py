"""与 AstrBot 运行时无关的纯逻辑，方便单独测试。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

RESERVED_DEFAULT_PERSONA = "default"


@dataclass(frozen=True)
class TargetResolution:
    """一次「切换目标」的解析结果。"""

    persona_id: str | None
    error: str | None = None
    ambiguous: tuple[str, ...] = ()


def unique_preserve(items: list[str]) -> list[str]:
    """去重并保持原顺序。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def effective_allowed(
    all_persona_ids: list[str],
    allowed_personas: list[str] | None,
    allow_all: bool,
) -> list[str]:
    """计算当前用户实际可切换的人格列表。"""
    if allow_all:
        return list(all_persona_ids)
    configured = set(unique_preserve([str(x).strip() for x in (allowed_personas or []) if str(x).strip()]))
    return [pid for pid in all_persona_ids if pid in configured]


def resolve_target(target: str, persona_ids: list[str]) -> TargetResolution:
    """把用户输入解析成人格 ID。

    顺序：精确匹配 → 忽略大小写 → 唯一的子串匹配。
    多个子串命中时返回 ambiguous，交给调用方提示用户写得更具体。
    """
    raw = (target or "").strip()
    if not raw:
        return TargetResolution(None, "没有指定人格")

    if raw in persona_ids:
        return TargetResolution(raw)

    lowered = raw.lower()
    case_hits = [pid for pid in persona_ids if pid.lower() == lowered]
    if len(case_hits) == 1:
        return TargetResolution(case_hits[0])

    contains_hits = [pid for pid in persona_ids if lowered in pid.lower()]
    if len(contains_hits) == 1:
        return TargetResolution(contains_hits[0])
    if len(contains_hits) > 1:
        return TargetResolution(None, "匹配到多个人格，请写得更具体", tuple(contains_hits))

    return TargetResolution(None, "没有找到这个人格")


def parse_index(target: str, persona_ids: list[str]) -> str | None:
    """按 1 开始的编号取人格 ID。"""
    raw = (target or "").strip()
    if not raw.isdigit():
        return None
    index = int(raw)
    if 1 <= index <= len(persona_ids):
        return persona_ids[index - 1]
    return None


def format_persona_line(index: int, persona_id: str, preview: str = "") -> str:
    """列表中的一行。"""
    line = f"{index}. {persona_id}"
    if preview:
        line += f" — {preview}"
    return line


def chunk_lines(lines: list[str], max_items: int) -> tuple[list[str], int]:
    """截断列表，返回展示行与剩余数量。"""
    if max_items <= 0 or len(lines) <= max_items:
        return lines, 0
    return lines[:max_items], len(lines) - max_items


def summarize_prompt(prompt: str | None, limit: int = 60) -> str:
    """取提示词第一段有效文字作预览。"""
    if not prompt:
        return ""
    for line in str(prompt).splitlines():
        text = line.strip()
        if text:
            return text if len(text) <= limit else text[:limit] + "…"
    return ""


def calc_vote_threshold(
    member_count: int,
    ratio: float = 0.02,
    min_votes: int = 3,
    max_votes: int = 20,
    fallback: int = 5,
) -> int:
    """按群人数算通过票数：ceil(人数 × 比例)，再夹在上下限之间。"""
    if member_count <= 0:
        return max(1, int(fallback))
    need = math.ceil(member_count * max(0.0, float(ratio)))
    need = max(need, int(min_votes))
    if max_votes and max_votes > 0:
        need = min(need, int(max_votes))
    need = min(need, member_count)
    return max(1, need)


AGREE_PATTERN = re.compile(r"同意|赞成|支持|附议|\+1|yes|agree|ok", re.IGNORECASE)
DISAGREE_PATTERN = re.compile(
    r"(?:不|没|别|拒绝|反对)\s*(?:同意|赞成|支持|附议)|不同意|反对",
    re.IGNORECASE,
)


def is_agree_text(text: str, keywords: list[str] | None = None) -> bool:
    """判断一条消息是不是赞成票，先排除「不同意」这类否定表达。"""
    raw = (text or "").strip()
    if not raw:
        return False
    if DISAGREE_PATTERN.search(raw):
        return False
    if keywords:
        lowered = raw.lower()
        return any(str(k).strip().lower() in lowered for k in keywords if str(k).strip())
    return bool(AGREE_PATTERN.search(raw))


def format_seconds(seconds: int) -> str:
    """把秒数变成人看的短文本。"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, rest = divmod(seconds, 60)
    if rest:
        return f"{minutes} 分 {rest} 秒"
    return f"{minutes} 分钟"
