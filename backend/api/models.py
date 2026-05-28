"""Shared models for Memory Hybrid V2."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LifecycleState(str, Enum):
    CAPTURED = "captured"
    PENDING = "pending"
    VERIFIED = "verified"
    CURATED = "curated"
    HARDENED = "hardened"
    DECAYED = "decayed"
    ARCHIVED = "archived"


class TaskMode(str, Enum):
    GENERAL = "general"
    DEBUG = "debug"
    IMPLEMENT = "implement"
    PLAN = "plan"
    SOCIAL = "social"


class TemporalMode(str, Enum):
    """Temporal scoring mode for recall queries."""
    AUTO = "auto"       # Classify from query text
    RECENT = "recent"   # Strong recency bias (latest first)
    PAST = "past"       # Reverse recency bias (older first)
    ALL = "all"         # No temporal bias
    CURRENT = "current" # Only items within 24h


class LifecycleMetadata(BaseModel):
    state: LifecycleState
    source_layer: str
    captured_at: str
    updated_at: str
    verification_count: int = 0
    decay_score: float = 0.0
    archived: bool = False


class HardeningCandidate(BaseModel):
    candidate_id: str
    source_decision_id: str
    trigger: str
    pattern: str
    corrective: str
    level: str = "soft"
    evidence_count: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)
