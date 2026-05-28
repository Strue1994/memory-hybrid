"""Promotion helpers for converting L6 decisions into L0 hardening candidates."""

from hashlib import sha1
from typing import Any

from .models import HardeningCandidate


def decision_to_candidate(decision_id: str, content: str, metadata: dict[str, Any]) -> HardeningCandidate:
    trigger = metadata.get("trigger", "decision_pattern")
    pattern = metadata.get("pattern") or content[:160]
    corrective = metadata.get("corrective", "Review and harden this repeated decision pattern")
    candidate_id = sha1(f"{decision_id}:{trigger}:{pattern}".encode()).hexdigest()[:16]
    return HardeningCandidate(
        candidate_id=candidate_id,
        source_decision_id=decision_id,
        trigger=trigger,
        pattern=pattern,
        corrective=corrective,
        level=metadata.get("level", "soft"),
        evidence_count=int(metadata.get("evidence_count", 1)),
        metadata=metadata,
    )
