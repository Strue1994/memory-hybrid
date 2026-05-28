"""Query classifier and task-aware router for Memory Hybrid V2."""

import re

from .models import TaskMode, TemporalMode


# Heuristic patterns
PATTERNS: list[tuple[str, set[str]]] = [
    # (regex_pattern, target_layers)
    (r"\b(今天|昨天|前天|明天|周[一二三四五六日天]|周[1-7]|周\b|\d{4}[-/]\d{1,2}[-/]\d{1,2}|刚刚|最近|之前|之前那[次个]|上次)\b", {"L3"}),
    (r"\b(张三|李四|王五|他|她|他们|我们|用户|同事|客户|老板|经理)\b", {"L2", "L3"}),
    (r"\b(怎么|如何|什么|为什么|是不是|能不能|会不会|原理|概念|定义|语法|API|函数|类|方法|工具)\b", {"L4"}),
    (r"\b(目标|计划|进度|完成|待办|TODO|里程碑|愿景|优先级|deadline|goal)\b", {"L5"}),
    (r"\b(为什么|决策|选择|原因|理由|当时|背景|权衡|方案|为什么没|决定)\b", {"L6", "L3"}),
    (r"\b(谁|哪些人|哪个团队|什么关系|和谁一起|合作)\b", {"L2"}),
]


TASK_MODE_PRIORITIES = {
    TaskMode.GENERAL: ["L2", "L3", "L4", "L5", "L6"],
    TaskMode.DEBUG: ["L6", "L3", "L4", "L2", "L5"],
    TaskMode.IMPLEMENT: ["L4", "L3", "L5", "L6", "L2"],
    TaskMode.PLAN: ["L5", "L6", "L4", "L3", "L2"],
    TaskMode.SOCIAL: ["L2", "L3", "L6", "L5", "L4"],
}


def classify_query(query: str, task_mode: TaskMode = TaskMode.GENERAL) -> list[str]:
    """Given a query string and task mode, return the ordered list of layer IDs to search."""
    matched: set[str] = set()
    for pattern, layers in PATTERNS:
        if re.search(pattern, query):
            matched.update(layers)

    priorities = TASK_MODE_PRIORITIES.get(task_mode, TASK_MODE_PRIORITIES[TaskMode.GENERAL])

    if not matched:
        return priorities

    return [layer for layer in priorities if layer in matched] + [
        layer for layer in priorities if layer not in matched and layer in {"L2", "L3", "L4", "L5", "L6"}
    ]


# ── Temporal Intent Classification ─────────────────────────────────


TEMPORAL_PATTERNS: list[tuple[str, TemporalMode]] = [
    # CJK patterns — NO trailing \b because adjacent CJK chars are also \w
    (r"(?:^|\b)(最近|刚刚|最新|昨天)", TemporalMode.RECENT),
    (r"(?:^|\b)(现在|当前|目前)", TemporalMode.CURRENT),
    (r"(?:^|\b)(以前|之前|过去|去年|上个月|曾经|曾|历史)", TemporalMode.PAST),
    (r"(?:^|\b)(所有|全部|全部历史)", TemporalMode.ALL),
    # English patterns — \b boundaries work for Latin text
    (r"\b(today|yesterday|latest|newest)\b", TemporalMode.RECENT),
    (r"\b(currently|now)\b", TemporalMode.CURRENT),
    (r"\b(last\s+(year|month|week)|formerly|previously|historical|originally)\b", TemporalMode.PAST),
    (r"\b(anytime|always|every|all\s+time)\b", TemporalMode.ALL),
]


def classify_temporal_intent(query: str) -> TemporalMode:
    """Classify the temporal intent of a query string.

    Returns a TemporalMode that can be used to bias recall scoring.
    When no temporal keywords are detected, returns AUTO (let caller decide).
    """
    for pattern, mode in TEMPORAL_PATTERNS:
        if re.search(pattern, query, re.IGNORECASE):
            return mode
    return TemporalMode.AUTO
