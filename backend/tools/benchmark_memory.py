"""Minimal benchmark for Memory Hybrid routing and lifecycle behavior."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(BACKEND_ROOT.parent))

from api.lifecycle import apply_decay, build_lifecycle
from api.models import LifecycleState, TaskMode
from api.promotion import decision_to_candidate
from api.router import classify_query


def _num(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def run_benchmark() -> dict[str, object]:
    cases_path = Path(__file__).resolve().with_name("benchmark_cases.json")
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    scenarios: dict[str, bool] = {}
    debug_route: list[str] = []
    implement_route: list[str] = []
    decayed_state = ""
    candidate_id = ""
    usefulness_hits = 0
    usefulness_total = 0
    false_hits = 0
    false_total = 0

    for case in cases:
        kind = case["kind"]
        name = case["name"]
        if kind == "route":
            route = classify_query(case["query"], TaskMode(case["task_mode"]))
            scenarios[name] = route[0] == case["expected_first"]
            useful_layers = set(case.get("useful_layers", []))
            misleading_layers = set(case.get("misleading_layers", []))
            usefulness_hits += sum(1 for layer in route[:2] if layer in useful_layers)
            usefulness_total += max(1, len(useful_layers))
            false_hits += sum(1 for layer in route[:2] if layer in misleading_layers)
            false_total += max(1, len(misleading_layers))
            if case["task_mode"] == "debug":
                debug_route = route
            if case["task_mode"] == "implement":
                implement_route = route
        elif kind == "lifecycle":
            lifecycle = build_lifecycle(case["source_layer"], LifecycleState(case["initial_state"]))
            decayed = apply_decay(lifecycle, float(case["decay_delta"]))
            decayed_state = decayed["state"]
            scenarios[name] = decayed_state == case["expected_state"]
        elif kind == "promotion":
            candidate_a = decision_to_candidate(case["decision_id"], case["content"], {"pattern": case["pattern"]})
            candidate_b = decision_to_candidate(case["decision_id"], case["content"], {"pattern": case["pattern"]})
            candidate_id = candidate_a.candidate_id
            scenarios[name] = candidate_a.candidate_id == candidate_b.candidate_id

    return {
        "passed": sum(1 for ok in scenarios.values() if ok),
        "total": len(scenarios),
        "scenarios": scenarios,
        "debug_route": debug_route,
        "implement_route": implement_route,
        "decayed_state": decayed_state,
        "candidate_id": candidate_id,
        "route_hit_rate": round(sum(1 for ok in scenarios.values() if ok) / max(1, len(scenarios)), 3),
        "usefulness_score": round(usefulness_hits / max(1, usefulness_total), 3),
        "false_helpfulness_score": round(false_hits / max(1, false_total), 3),
    }


def report_dir() -> Path:
    output_dir = Path(__file__).resolve().with_name("reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def prune_reports(limit: int = 10) -> dict[str, int]:
    reports = sorted(report_dir().glob("benchmark-*.json"))
    markdowns = sorted(report_dir().glob("benchmark-*.md"))
    removed_json = 0
    removed_md = 0
    for path in reports[:-limit]:
        path.unlink(missing_ok=True)
        removed_json += 1
    for path in markdowns[:-limit]:
        path.unlink(missing_ok=True)
        removed_md += 1
    return {
        "removed_json": removed_json,
        "removed_markdown": removed_md,
        "remaining_json": len(list(report_dir().glob("benchmark-*.json"))),
        "remaining_markdown": len(list(report_dir().glob("benchmark-*.md"))),
    }


def persist_markdown_summary(report: dict[str, object], json_report_path: Path) -> Path:
    md_path = json_report_path.with_suffix(".md")
    trends = report.get("trends", {})
    recent_trends = report.get("recent_trends", {})
    regressions = report.get("regressions", {})
    alerts = report.get("alerts", {})
    lines = [
        "# Memory Hybrid Benchmark Summary",
        "",
        f"- passed: {report['passed']}/{report['total']}",
        f"- route_hit_rate: {report['route_hit_rate']}",
        f"- usefulness_score: {report['usefulness_score']}",
        f"- false_helpfulness_score: {report['false_helpfulness_score']}",
        f"- regressions: {regressions}",
        f"- alerts: {alerts}",
        "",
        "## Trend Summary",
        f"- all-time: {trends}",
        f"- recent-window: {recent_trends}",
        "",
        "## Interpretation",
        f"- route health: {'regressed' if regressions.get('route_hit_regressed') else 'stable'}",
        f"- usefulness health: {'regressed' if regressions.get('usefulness_regressed') else 'stable'}",
        f"- false-helpfulness health: {'worse' if regressions.get('false_helpfulness_regressed') else 'stable'}",
        f"- alert summary: {'attention needed' if any(alerts.values()) else 'no alert'}",
        "",
        "## Historical Comparison",
        f"| Metric | All-time | Recent |",
        f"|---|---:|---:|",
        f"| route_hit_rate | {trends.get('avg_route_hit_rate')} | {recent_trends.get('avg_route_hit_rate')} |",
        f"| usefulness_score | {trends.get('avg_usefulness_score')} | {recent_trends.get('avg_usefulness_score')} |",
        f"| false_helpfulness_score | {trends.get('avg_false_helpfulness_score')} | {recent_trends.get('avg_false_helpfulness_score')} |",
        "",
        "## Routes",
        f"- debug: {report['debug_route']}",
        f"- implement: {report['implement_route']}",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def persist_report(report: dict[str, object]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = report_dir() / f"benchmark-{timestamp}.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def summarize_reports() -> dict[str, object]:
    reports = []
    for path in sorted(report_dir().glob("benchmark-*.json")):
        reports.append(json.loads(path.read_text(encoding="utf-8")))
    if not reports:
        return {"report_count": 0}
    return {
        "report_count": len(reports),
        "avg_route_hit_rate": round(sum(_num(r.get("route_hit_rate", 0.0)) for r in reports) / len(reports), 3),
        "avg_usefulness_score": round(sum(_num(r.get("usefulness_score", 0.0)) for r in reports) / len(reports), 3),
        "avg_false_helpfulness_score": round(sum(_num(r.get("false_helpfulness_score", 0.0)) for r in reports) / len(reports), 3),
    }


def summarize_recent_reports(limit: int = 3) -> dict[str, object]:
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(report_dir().glob("benchmark-*.json"))[-limit:]]
    if not reports:
        return {"report_count": 0}
    return {
        "report_count": len(reports),
        "avg_route_hit_rate": round(sum(_num(r.get("route_hit_rate", 0.0)) for r in reports) / len(reports), 3),
        "avg_usefulness_score": round(sum(_num(r.get("usefulness_score", 0.0)) for r in reports) / len(reports), 3),
        "avg_false_helpfulness_score": round(sum(_num(r.get("false_helpfulness_score", 0.0)) for r in reports) / len(reports), 3),
        "window": limit,
    }


def detect_regressions(trends: dict[str, object], recent_trends: dict[str, object]) -> dict[str, bool]:
    if not trends.get("report_count") or not recent_trends.get("report_count"):
        return {"route_hit_regressed": False, "usefulness_regressed": False, "false_helpfulness_regressed": False}
    return {
        "route_hit_regressed": _num(recent_trends.get("avg_route_hit_rate", 0.0)) < _num(trends.get("avg_route_hit_rate", 0.0)),
        "usefulness_regressed": _num(recent_trends.get("avg_usefulness_score", 0.0)) < _num(trends.get("avg_usefulness_score", 0.0)),
        "false_helpfulness_regressed": _num(recent_trends.get("avg_false_helpfulness_score", 0.0)) > _num(trends.get("avg_false_helpfulness_score", 0.0)),
    }


def detect_alerts(recent_trends: dict[str, object]) -> dict[str, bool]:
    return {
        "route_hit_alert": _num(recent_trends.get("avg_route_hit_rate", 0.0)) < 0.8,
        "usefulness_alert": _num(recent_trends.get("avg_usefulness_score", 0.0)) < 0.8,
        "false_helpfulness_alert": _num(recent_trends.get("avg_false_helpfulness_score", 0.0)) > 0.2,
    }


def summarize_stability(regressions: dict[str, bool]) -> dict[str, list[str]]:
    stable = [name for name, flag in regressions.items() if not flag]
    regressed = [name for name, flag in regressions.items() if flag]
    return {"top_stable": stable[:3], "top_regressions": regressed[:3]}


if __name__ == "__main__":
    report = run_benchmark()
    report["trends"] = summarize_reports()
    report["recent_trends"] = summarize_recent_reports()
    report["regressions"] = detect_regressions(report["trends"], report["recent_trends"])
    report["alerts"] = detect_alerts(report["recent_trends"])
    report["stability_summary"] = summarize_stability(report["regressions"])
    output_path = persist_report(report)
    markdown_path = persist_markdown_summary(report, output_path)
    report["cleanup"] = prune_reports()
    report["report_path"] = str(output_path)
    report["markdown_report_path"] = str(markdown_path)
    print(json.dumps(report, ensure_ascii=False))
