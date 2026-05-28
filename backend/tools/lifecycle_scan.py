"""Scan lifecycle records and emit decay/archive transitions."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


AUTO_APPROVE_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def _project_root() -> Path:
    """Return the memory-hybrid project root directory.

    Priority:
    1. MEMORY_HYBRID_ROOT env var
    2. MEMORY_ROOT env var (legacy)
    3. Computed from __file__ location
    """
    env = os.environ.get("MEMORY_HYBRID_ROOT") or os.environ.get("MEMORY_ROOT")
    if env:
        return Path(env).resolve()
    # backend/tools/lifecycle_scan.py -> parents[2] = project root
    return Path(__file__).resolve().parents[2]


def _preferred_scan_root() -> Path | None:
    """Return the effective data root for scanning.

    Priority:
    1. MEMORY_HYBRID_ROOT env var
    2. MEMORY_ROOT env var (legacy)
    3. sibling "memory-hybrid/" directory (skill mode)
    4. project root itself (standalone mode)
    """
    env = os.environ.get("MEMORY_HYBRID_ROOT") or os.environ.get("MEMORY_ROOT")
    if env:
        return Path(env).resolve()
    skill_candidate = _project_root() / "memory-hybrid"
    if skill_candidate.exists():
        return skill_candidate
    candidate = _project_root()
    return candidate if candidate.exists() else None


def _remediation_dir(scan_root: Path | None) -> Path | None:
    if scan_root is None:
        return None
    return scan_root / "remediation-candidates"


def _remediation_candidate(action: dict[str, str], idx: int, auto_approve_threshold: str = "high") -> dict[str, Any]:
    candidate_id = f"remediation-{idx}"
    severity = "high" if action["action"] == "archive-migration" else "medium"
    priority = "high" if severity == "high" else "medium"
    auto_approve = AUTO_APPROVE_SEVERITY_ORDER.get(severity, 0) >= AUTO_APPROVE_SEVERITY_ORDER.get(auto_approve_threshold, 2)
    return {
        "candidate_id": candidate_id,
        "source_decision_id": f"SCAN-{candidate_id}",
        "trigger": action["action"],
        "pattern": action["target"],
        "corrective": action["action"],
        "level": "soft",
        "severity": severity,
        "priority": priority,
        "auto_approve_recommended": auto_approve,
        "evidence_count": 1,
        "metadata": {"origin": "lifecycle-scan"},
        "status": "pending",
    }


YAML_BLOCK_RE = re.compile(r"```yaml\s*(.*?)```", re.DOTALL)


DECAY_THRESHOLD = 1.0
ARCHIVE_AGE_DAYS = 7


def _parse_iso(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError("unsupported datetime value")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def classify_item(item: dict[str, Any]) -> str:
    lifecycle = item.get("lifecycle", {})
    state = lifecycle.get("state", "captured")
    decay_score = float(lifecycle.get("decay_score", 0.0))
    updated_at = lifecycle.get("updated_at") or lifecycle.get("captured_at")
    if decay_score >= DECAY_THRESHOLD and state != "archived":
        if state == "decayed" and updated_at:
            age_days = (_now() - _parse_iso(updated_at)).days
            if age_days >= ARCHIVE_AGE_DAYS:
                return "archived"
        return "decayed"
    return "untouched"


def _extract_records(data: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            records.extend(_extract_records(item))
        return records
    if isinstance(data, dict):
        if "lifecycle" in data:
            records.append(data)
        for value in data.values():
            if isinstance(value, (list, dict)):
                records.extend(_extract_records(value))
    return records


def _extract_markdown_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for match in YAML_BLOCK_RE.findall(text):
        parsed = yaml.safe_load(match)
        records.extend(_extract_records(parsed))
    return records


def _infer_type_from_id(item_id: str) -> str:
    if item_id.startswith("timeline"):
        return "timeline"
    if item_id.startswith("goal"):
        return "goal"
    if item_id.startswith("fact"):
        return "fact"
    if item_id.startswith("decision"):
        return "decision"
    return "unknown"


def load_records(scan_root: Path | None = None) -> list[dict[str, Any]]:
    if scan_root and scan_root.exists():
        records: list[dict[str, Any]] = []
        for file_path in scan_root.rglob("*"):
            if file_path.suffix.lower() not in {".json", ".yaml", ".yml", ".md"}:
                continue
            if file_path.suffix.lower() == ".json":
                data = json.loads(file_path.read_text(encoding="utf-8"))
                records.extend(_extract_records(data))
            elif file_path.suffix.lower() == ".md":
                records.extend(_extract_markdown_records(file_path.read_text(encoding="utf-8")))
            else:
                data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
                records.extend(_extract_records(data))
        return records

    records_path = Path(__file__).resolve().with_name("lifecycle_samples.json")
    if not records_path.exists():
        return []
    return json.loads(records_path.read_text(encoding="utf-8"))


def main() -> None:
    scan_root = Path(sys.argv[1]) if len(sys.argv) > 1 else _preferred_scan_root()
    records = load_records(scan_root)
    result: dict[str, list[str]] = {"decayed": [], "archived": [], "untouched": []}
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for item in records:
        bucket = classify_item(item)
        item_id = item.get("id", "unknown")
        result[bucket].append(item_id)
        item_type = _infer_type_from_id(item_id)
        by_type[item_type] = by_type.get(item_type, 0) + 1
    summary = {
        "total_records": len(records),
        "decayed_count": len(result["decayed"]),
        "archived_count": len(result["archived"]),
        "untouched_count": len(result["untouched"]),
    }
    recommendations: list[str] = []
    action_plan: list[dict[str, str]] = []
    if result["decayed"]:
        recommendations.append("Review decayed items for curation or removal")
        action_plan.append({"action": "review-decayed", "target": ",".join(result["decayed"][:5])})
    if result["archived"]:
        recommendations.append("Move archived items to long-term storage or compress them")
        action_plan.append({"action": "archive-migration", "target": ",".join(result["archived"][:5])})
    if not recommendations:
        recommendations.append("No lifecycle migration actions needed")
        action_plan.append({"action": "noop", "target": "none"})
    payload = {
        **result,
        "summary": summary,
        "by_type": by_type,
        "recommendations": recommendations,
        "action_plan": action_plan,
    }
    remediation_dir = _remediation_dir(scan_root)
    generated: list[str] = []
    if remediation_dir is not None and action_plan:
        remediation_dir.mkdir(parents=True, exist_ok=True)
        for idx, action in enumerate(action_plan, start=1):
            output_path = remediation_dir / f"remediation-{idx}.yaml"
            candidate = _remediation_candidate(action, idx)
            severity = str(candidate.get("severity", "medium"))
            by_severity[severity] = by_severity.get(severity, 0) + 1
            output_path.write_text(
                yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            generated.append(str(output_path))
    payload["by_severity"] = by_severity
    payload["generated_candidates"] = generated
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
