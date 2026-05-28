"""Lifecycle helpers for Memory Hybrid V2."""

from datetime import datetime, timezone
from typing import Any

from .models import LifecycleMetadata, LifecycleState


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_lifecycle(source_layer: str, state: LifecycleState) -> dict[str, Any]:
    now = now_iso()
    return LifecycleMetadata(
        state=state,
        source_layer=source_layer,
        captured_at=now,
        updated_at=now,
    ).model_dump()


def transition_lifecycle(metadata: dict[str, Any], new_state: LifecycleState) -> dict[str, Any]:
    current = LifecycleMetadata(**metadata)
    updated = current.model_copy(update={"state": new_state, "updated_at": now_iso()})
    return updated.model_dump()


def apply_decay(metadata: dict[str, Any], score_delta: float) -> dict[str, Any]:
    current = LifecycleMetadata(**metadata)
    decayed = current.model_copy(
        update={
            "decay_score": max(0.0, current.decay_score + score_delta),
            "updated_at": now_iso(),
        }
    )
    if decayed.decay_score >= 1.0:
        decayed = decayed.model_copy(update={"state": LifecycleState.DECAYED})
    return decayed.model_dump()
