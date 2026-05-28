"""File-backed review flow for hardening candidates."""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .models import HardeningCandidate


def _project_root() -> Path:
    """Return the memory-hybrid project root directory.

    Priority:
    1. MEMORY_HYBRID_ROOT env var
    2. MEMORY_ROOT env var (legacy)
    3. Computed from __file__ location (works both as skill and standalone clone)
    """
    env = os.environ.get("MEMORY_HYBRID_ROOT") or os.environ.get("MEMORY_ROOT")
    if env:
        return Path(env).resolve()
    # backend/api/review_flow.py -> parents[2] = project root
    return Path(__file__).resolve().parents[2]


def candidate_review_dir() -> Path:
    return _project_root() / "hardening" / "candidates"


def rules_file_path() -> Path:
    return _project_root() / "hardening" / "rules.yaml"


def review_log_path() -> Path:
    return _project_root() / "hardening" / "review-log.yaml"


def rules_history_dir() -> Path:
    return _project_root() / "hardening" / "history"


def _log_review_action(candidate_id: str, action: str) -> None:
    log_path = review_log_path()
    existing = yaml.safe_load(log_path.read_text(encoding="utf-8")) if log_path.exists() else {"events": []}
    events = existing.get("events") or []
    events.append(
        {
            "candidate_id": candidate_id,
            "action": action,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    existing["events"] = events
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _snapshot_rules(reason: str) -> Path:
    rules_path = rules_file_path()
    history_dir = rules_history_dir()
    history_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = history_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{reason}.yaml"
    payload = rules_path.read_text(encoding="utf-8") if rules_path.exists() else yaml.safe_dump({"rules": []}, allow_unicode=True, sort_keys=False)
    snapshot_path.write_text(payload, encoding="utf-8")
    return snapshot_path


def write_candidate_review(candidate: HardeningCandidate) -> Path:
    review_dir = candidate_review_dir()
    review_dir.mkdir(parents=True, exist_ok=True)
    output_path = review_dir / f"{candidate.candidate_id}.yaml"
    payload = candidate.model_dump()
    payload["status"] = "pending"
    with output_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
    return output_path


def read_candidate_review(candidate_id: str) -> dict[str, Any]:
    search_paths = [candidate_review_dir() / f"{candidate_id}.yaml"]
    remediation_root = preferred_memory_root()
    if remediation_root:
        search_paths.append(remediation_root / "remediation-candidates" / f"{candidate_id}.yaml")
    search_paths.extend(_project_root().rglob(f"remediation-candidates/{candidate_id}.yaml"))
    for review_path in search_paths:
        if review_path.exists():
            return yaml.safe_load(review_path.read_text(encoding="utf-8")) or {}
    return {}


def _write_candidate_payload(candidate_id: str, payload: dict[str, Any]) -> Path:
    candidate_path = candidate_review_dir() / f"{candidate_id}.yaml"
    remediation_root = preferred_memory_root()
    remediation_path = remediation_root / "remediation-candidates" / f"{candidate_id}.yaml" if remediation_root else None
    existing_paths = [candidate_path]
    if remediation_path:
        existing_paths.append(remediation_path)
    existing_paths.extend(_project_root().rglob(f"remediation-candidates/{candidate_id}.yaml"))
    review_path = next((path for path in existing_paths if path.exists()), candidate_path)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    with review_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
    return review_path


def approve_candidate(candidate_id: str) -> dict[str, Any]:
    payload = read_candidate_review(candidate_id)
    if payload.get("status") == "approved":
        _log_review_action(candidate_id, "approve-noop")
        return payload
    payload["status"] = "approved"
    rules_path = rules_file_path()
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    _snapshot_rules(f"approve-{candidate_id}")
    existing = yaml.safe_load(rules_path.read_text(encoding="utf-8")) if rules_path.exists() else {"rules": []}
    rules = existing.get("rules") or []
    rule_id = f"HARDEN-{candidate_id}"
    if not any(rule.get("id") == rule_id for rule in rules):
        rules.append(
            {
                "id": rule_id,
                "trigger": payload["trigger"],
                "pattern": payload["pattern"],
                "corrective": payload["corrective"],
                "level": payload.get("level", "soft"),
                "enabled": True,
                "source_decision_id": payload["source_decision_id"],
                "evidence_count": payload.get("evidence_count", 1),
            }
        )
    existing["rules"] = rules
    rules_path.write_text(yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _write_candidate_payload(candidate_id, payload)
    _log_review_action(candidate_id, "approved")
    return payload


def reject_candidate(candidate_id: str) -> dict[str, Any]:
    payload = read_candidate_review(candidate_id)
    payload["status"] = "rejected"
    _write_candidate_payload(candidate_id, payload)
    _log_review_action(candidate_id, "rejected")
    return payload


def rollback_rules() -> dict[str, Any]:
    history_dir = rules_history_dir()
    snapshots = sorted(history_dir.glob("*.yaml"))
    if not snapshots:
        return {"rolled_back": False, "reason": "no_snapshots"}
    latest = snapshots[-1]
    current = yaml.safe_load(rules_file_path().read_text(encoding="utf-8")) if rules_file_path().exists() else {"rules": []}
    rules_file_path().write_text(latest.read_text(encoding="utf-8"), encoding="utf-8")
    restored = yaml.safe_load(rules_file_path().read_text(encoding="utf-8")) if rules_file_path().exists() else {"rules": []}
    return {
        "rolled_back": True,
        "snapshot": str(latest),
        "before_rules": len((current or {}).get("rules", [])),
        "after_rules": len((restored or {}).get("rules", [])),
        "snapshot_count": len(snapshots),
    }


def list_candidate_reviews() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(candidate_review_dir().glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        payload["path"] = str(path)
        items.append(payload)
    remediation_root = preferred_memory_root()
    remediation_dir = remediation_root / "remediation-candidates" if remediation_root else None
    if remediation_dir and remediation_dir.exists():
        for path in sorted(remediation_dir.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            payload["path"] = str(path)
            items.append(payload)
    for path in sorted(_project_root().rglob("remediation-candidates/*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        payload["path"] = str(path)
        if not any(existing.get("path") == payload["path"] for existing in items):
            items.append(payload)
    return items


def filter_candidate_reviews(status: str | None = None, candidate_id: str | None = None) -> list[dict[str, Any]]:
    items = list_candidate_reviews()
    if status:
        items = [item for item in items if item.get("status") == status]
    if candidate_id:
        items = [item for item in items if item.get("candidate_id") == candidate_id]
    return items


def sort_candidates(items: list[dict[str, Any]], by_priority: bool = False) -> list[dict[str, Any]]:
    if not by_priority:
        return items
    severity_order = {"high": 0, "medium": 1, "low": 2}
    priority_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        items,
        key=lambda item: (
            severity_order.get(str(item.get("severity", "medium")), 9),
            priority_order.get(str(item.get("priority", "medium")), 9),
            str(item.get("candidate_id", "")),
        ),
    )


def slice_items(items: list[dict[str, Any]], offset: int = 0, limit: int | None = None, recent: int | None = None) -> list[dict[str, Any]]:
    if recent is not None:
        items = items[-recent:]
    if offset:
        items = items[offset:]
    if limit is not None:
        items = items[:limit]
    return items


def _within_range(value: str | None, since: str | None = None, until: str | None = None) -> bool:
    if not value:
        return False
    current = datetime.fromisoformat(value)
    if since and current < datetime.fromisoformat(since):
        return False
    if until and current > datetime.fromisoformat(until):
        return False
    return True


def read_review_log() -> dict[str, Any]:
    if not review_log_path().exists():
        return {"events": []}
    return yaml.safe_load(review_log_path().read_text(encoding="utf-8")) or {"events": []}


def filter_review_log(candidate_id: str | None = None, action: str | None = None, since: str | None = None, until: str | None = None, q: str | None = None) -> dict[str, Any]:
    payload = read_review_log()
    events = payload.get("events") or []
    if candidate_id:
        events = [event for event in events if event.get("candidate_id") == candidate_id]
    if action:
        events = [event for event in events if event.get("action") == action]
    if since or until:
        events = [event for event in events if _within_range(event.get("timestamp"), since=since, until=until)]
    if q:
        q_lower = q.lower()
        events = [event for event in events if q_lower in str(event).lower()]
    return {"events": events}


def list_rule_snapshots(since: str | None = None, until: str | None = None) -> list[str]:
    paths = sorted(rules_history_dir().glob("*.yaml"))
    if since or until:
        filtered: list[Path] = []
        for path in paths:
            name = path.stem.split("-", 1)[0]
            stamp = datetime.strptime(name, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat()
            if _within_range(stamp, since=since, until=until):
                filtered.append(path)
        paths = filtered
    return [str(path) for path in paths]


def diff_rule_snapshots(snapshot_a: str | None = None, snapshot_b: str | None = None) -> dict[str, Any]:
    snapshots = list_rule_snapshots()
    if not snapshots:
        return {"added": [], "removed": [], "changed": []}
    if snapshot_a is None and len(snapshots) >= 2:
        snapshot_a = snapshots[-2]
    if snapshot_b is None:
        snapshot_b = snapshots[-1]
    data_a = yaml.safe_load(Path(snapshot_a).read_text(encoding="utf-8")) if snapshot_a else {"rules": []}
    data_b = yaml.safe_load(Path(snapshot_b).read_text(encoding="utf-8")) if snapshot_b else {"rules": []}
    rules_a = {rule.get("id"): rule for rule in (data_a.get("rules") or [])}
    rules_b = {rule.get("id"): rule for rule in (data_b.get("rules") or [])}
    added = [rule_id for rule_id in rules_b if rule_id not in rules_a]
    removed = [rule_id for rule_id in rules_a if rule_id not in rules_b]
    changed = [rule_id for rule_id in rules_a if rule_id in rules_b and rules_a[rule_id] != rules_b[rule_id]]
    return {
        "snapshot_a": snapshot_a,
        "snapshot_b": snapshot_b,
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def set_rule_enabled(rule_id: str, enabled: bool, reason: str | None = None) -> dict[str, Any]:
    rules_path = rules_file_path()
    payload = yaml.safe_load(rules_path.read_text(encoding="utf-8")) if rules_path.exists() else {"rules": []}
    rules = payload.get("rules") or []
    for rule in rules:
        if rule.get("id") == rule_id:
            rule["enabled"] = enabled
            if not enabled and reason:
                rule["disabled_reason"] = reason
            elif enabled:
                rule.pop("disabled_reason", None)
            rules_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            _log_review_action(rule_id, "enabled" if enabled else "disabled")
            return rule
    return {"id": rule_id, "enabled": enabled, "found": False}


def candidate_rule_links() -> list[dict[str, Any]]:
    candidates = list_candidate_reviews()
    payload = yaml.safe_load(rules_file_path().read_text(encoding="utf-8")) if rules_file_path().exists() else {"rules": []}
    rules = payload.get("rules") or []
    linked = []
    for candidate in candidates:
        rule_id = f"HARDEN-{candidate.get('candidate_id')}"
        match = next((rule for rule in rules if rule.get("id") == rule_id), None)
        linked.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "candidate_status": candidate.get("status"),
                "rule_id": rule_id,
                "rule_exists": match is not None,
                "rule_enabled": match.get("enabled") if match else None,
            }
        )
    return linked


def list_rules(enabled: bool | None = None, q: str | None = None) -> list[dict[str, Any]]:
    payload = yaml.safe_load(rules_file_path().read_text(encoding="utf-8")) if rules_file_path().exists() else {"rules": []}
    rules = payload.get("rules") or []
    if enabled is not None:
        rules = [rule for rule in rules if bool(rule.get("enabled", True)) is enabled]
    if q:
        q_lower = q.lower()
        rules = [rule for rule in rules if q_lower in str(rule).lower()]
    return rules


def unified_timeline() -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for candidate in list_candidate_reviews():
        timeline.append(
            {
                "kind": "candidate",
                "id": candidate.get("candidate_id"),
                "timestamp": candidate.get("metadata", {}).get("created_at") or candidate.get("metadata", {}).get("captured_at") or "",
                "payload": candidate,
            }
        )
    for event in read_review_log().get("events", []):
        timeline.append(
            {
                "kind": "review_event",
                "id": event.get("candidate_id"),
                "timestamp": event.get("timestamp", ""),
                "payload": event,
            }
        )
    for rule in list_rules():
        timeline.append(
            {
                "kind": "rule",
                "id": rule.get("id"),
                "timestamp": "",
                "payload": rule,
            }
        )
    return sorted(timeline, key=lambda item: str(item.get("timestamp", "")), reverse=True)


def _data_root() -> Path:
    """Return the effective memory data root directory.

    Priority:
    1. MEMORY_HYBRID_ROOT env var
    2. MEMORY_ROOT env var (legacy)
    3. _project_root() / "memory-hybrid"  (skill mode: sibling to project)
    4. _project_root() itself             (standalone mode)
    """
    env = os.environ.get("MEMORY_HYBRID_ROOT") or os.environ.get("MEMORY_ROOT")
    if env:
        return Path(env).resolve()
    skill_candidate = _project_root() / "memory-hybrid"
    if skill_candidate.exists():
        return skill_candidate
    return _project_root()


def preferred_memory_root() -> Path | None:
    root = _data_root()
    return root if root.exists() else None


def ensure_memory_root() -> Path:
    root = _data_root()
    for rel in [
        "hardening/candidates",
        "hardening/history",
        "sessions/sessions-archive",
        "profiles/humans",
        "profiles/agents",
        "timeline",
        "facts/curated",
        "facts/pending",
        "goals",
        "decisions",
        "state-machines",
    ]:
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root
