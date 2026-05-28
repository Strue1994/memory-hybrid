"""Task-aware scoring helpers for unified recall."""

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Protocol

from .models import TaskMode, TemporalMode


class RecallItemLike(Protocol):
    layer: str
    score: float
    content: str
    metadata: dict[str, object]


TASK_MODE_WEIGHTS = {
    TaskMode.GENERAL: {"L2": 0.8, "L3": 1.0, "L4": 1.2, "L5": 0.6, "L6": 0.7},
    TaskMode.DEBUG: {"L2": 0.5, "L3": 1.1, "L4": 0.9, "L5": 0.4, "L6": 1.4},
    TaskMode.IMPLEMENT: {"L2": 0.4, "L3": 1.0, "L4": 1.4, "L5": 0.8, "L6": 0.7},
    TaskMode.PLAN: {"L2": 0.5, "L3": 0.8, "L4": 1.0, "L5": 1.3, "L6": 1.1},
    TaskMode.SOCIAL: {"L2": 1.4, "L3": 1.0, "L4": 0.7, "L5": 0.8, "L6": 0.9},
}


def recency_bonus(created_str: str) -> float:
    if not created_str:
        return 0.0
    created = _parse_iso(created_str)
    if created is None:
        return 0.0
    hours_ago = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if hours_ago < 24:
        return 0.3
    if hours_ago < 168:
        return 0.1
    return 0.0


def score_item(item: RecallItemLike, task_mode: TaskMode) -> RecallItemLike:
    weight = TASK_MODE_WEIGHTS.get(task_mode, TASK_MODE_WEIGHTS[TaskMode.GENERAL]).get(item.layer, 1.0)
    created_value = item.metadata.get("created_at", "")
    created_str = created_value if isinstance(created_value, str) else ""
    item.score = weight * item.score + recency_bonus(created_str)
    return item


def normalize_content(text: str) -> str:
    return " ".join((text or "").lower().split())


def dedupe_ranked(items: Sequence[RecallItemLike]) -> list[RecallItemLike]:
    seen: set[str] = set()
    deduped: list[RecallItemLike] = []
    for item in sorted(items, key=lambda x: x.score, reverse=True):
        key = normalize_content(item.content)
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


# ── Temporal Scoring (Phase 31) ────────────────────────────────────


def _parse_iso(created_str: str) -> datetime | None:
    """Safely parse an ISO datetime string, returning None on failure.
    Naive datetimes are assumed to be UTC (same convention used throughout the API).
    """
    if not created_str:
        return None
    try:
        dt = datetime.fromisoformat(created_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def score_temporal(
    created_str: str,
    mode: TemporalMode = TemporalMode.AUTO,
    base_score: float = 1.0,
) -> float:
    """Compute a temporal bonus multiplier for a memory item.

    Args:
        created_str: ISO datetime string from metadata.created_at.
        mode: Temporal scoring mode.
        base_score: The item's existing score (task-weighted).

    Returns:
        Multiplied score with temporal bias applied.
        Guaranteed >= 0 and typically within [0.5 * base, 2.0 * base].
    """
    created = _parse_iso(created_str)
    if created is None:
        return base_score

    hours_ago = (datetime.now(timezone.utc) - created).total_seconds() / 3600

    if mode == TemporalMode.RECENT:
        # Exponential decay: half-life of 7 days (168 hours)
        # Items from seconds ago get ~1.5x boost; 7-day-old items neutral; older decays
        bonus = 0.5 * (2.0 ** (-hours_ago / 168))
        return base_score * (1.0 + bonus)

    elif mode == TemporalMode.PAST:
        # S-curve favoring older items: ramp over 30 days (720 hours)
        bonus = 0.3 * (1.0 - 2.0 ** (-hours_ago / 720))
        return base_score * (1.0 + bonus)

    elif mode == TemporalMode.CURRENT:
        # Strict: only items within 24h get a boost
        if hours_ago < 24:
            return base_score * 1.5
        return base_score * 0.3  # heavily penalise old items

    elif mode == TemporalMode.ALL:
        return base_score  # no temporal bias

    # AUTO: use existing recency_bonus logic
    return base_score * (1.0 + recency_bonus(created_str))
