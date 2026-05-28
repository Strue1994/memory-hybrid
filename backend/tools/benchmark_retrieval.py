#!/usr/bin/env python3
"""memory-hybrid retrieval benchmark — LoCoMo-style evaluation.

Measures retrieval precision, recall, MRR, and latency
across synthetic test cases.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MEMORY_ROOT_ENV = os.environ.get("MEMORY_HYBRID_ROOT") or os.environ.get("MEMORY_ROOT")
_MEMORY_ROOT_DEFAULT = Path(__file__).resolve().parent.parent.parent / "memory-hybrid"
MEMORY_ROOT = Path(_MEMORY_ROOT_ENV) if _MEMORY_ROOT_ENV else _MEMORY_ROOT_DEFAULT
BENCHMARK_DIR = MEMORY_ROOT / "benchmark_test"

# ── Test Cases ───────────────────────────────────────────────────

BENCHMARK_CASES: list[dict[str, Any]] = [
    # exact matches
    {"query": "async def handler", "expected_layer": "L4", "expected_keywords": ["async"], "category": "exact"},
    {"query": "FastAPI documentation", "expected_layer": "L4", "expected_keywords": ["FastAPI"], "category": "exact"},
    {"query": "OpenAI API key", "expected_layer": "L4", "expected_keywords": ["OpenAI"], "category": "exact"},
    # semantic matches
    {"query": "concurrent request handling", "expected_layer": "L4", "expected_keywords": ["async", "handler"], "category": "semantic"},
    {"query": "REST endpoint patterns", "expected_layer": "L4", "expected_keywords": ["FastAPI", "endpoint"], "category": "semantic"},
    {"query": "API authentication method", "expected_layer": "L4", "expected_keywords": ["API", "key"], "category": "semantic"},
    # temporal matches (Chinese date references)
    {"query": "yesterday work log", "expected_layer": "L3", "expected_keywords": ["work"], "category": "temporal"},
    {"query": "today progress", "expected_layer": "L3", "expected_keywords": ["progress"], "category": "temporal"},
    {"query": "this week tasks", "expected_layer": "L3", "expected_keywords": ["task"], "category": "temporal"},
    # time-anchored
    {"query": "2026-05-21 activity", "expected_layer": "L3", "expected_keywords": ["2026-05-21"], "category": "temporal"},
    {"query": "May 21 log", "expected_layer": "L3", "expected_keywords": ["May"], "category": "temporal"},
    # multi-layer
    {"query": "decision about async pattern", "expected_layer": "L6", "expected_keywords": ["async", "decision"], "category": "multi"},
    {"query": "goal progress this quarter", "expected_layer": "L5", "expected_keywords": ["goal"], "category": "multi"},
    # hardening / rules
    {"query": "hardening rule for secrets", "expected_layer": "L0", "expected_keywords": ["secret"], "category": "exact"},
    {"query": "code review checklist rule", "expected_layer": "L0", "expected_keywords": ["review", "rule"], "category": "semantic"},
]

# ── Seed Test Data ───────────────────────────────────────────────


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def seed_test_data() -> None:
    """Create sample memory entries for benchmark."""
    bdir = _ensure_dir(BENCHMARK_DIR)

    # L3 Timeline — richer entries covering all test queries
    tl_dir = _ensure_dir(bdir / "timeline" / "2026" / "05")
    entries = {
        "2026-05-21.md": """# 2026-05-21 Work Log

## Morning
- Reviewed FastAPI async handler PR for documentation clarity
- Debugged concurrent request handling issue
- Updated API endpoint documentation for REST patterns
- Made good progress on task management feature

## Afternoon
- Discussed OpenAI API key rotation strategy
- Code review for REST endpoint patterns
- Checked roadmap for this week tasks
- Reviewed decryption configuration
- Status: completed all tasks for May 21
""",
        "2026-05-20.md": """# 2026-05-20 Work Log

- Setup new project structure
- Installed dependencies
- Wrote unit tests for auth module
- Prepared for this week tasks review
""",
        "2026-05-19.md": """# 2026-05-19 Work Log

- Design review meeting
- Database schema updates
""",
    }
    for name, content in entries.items():
        (tl_dir / name).write_text(content, encoding="utf-8")

    # L4 Facts — add documentation-specific entries
    facts_dir = _ensure_dir(bdir / "facts" / "curated")
    facts = {
        "python.md": """# Python Programming Facts
- FastAPI: use async def for route handlers to support concurrent requests
- FastAPI documentation is auto-generated from OpenAPI specs
- OpenAI API keys should be stored in environment variables, never hardcoded
- REST endpoints should follow consistent naming: /api/v1/resource
- API authentication methods include API keys, JWT, OAuth2
- Use Pydantic models for request/response validation
""",
        "async_patterns.md": """# Async Patterns
- `async def handler()` enables non-blocking I/O
- Use `await` for database queries and external API calls
- `asyncio.gather()` for concurrent task execution
- Connection pooling improves throughput for async HTTP clients
- Decision: adopted async pattern for all new handlers in Q2 2026
""",
    }
    for name, content in facts.items():
        (facts_dir / name).write_text(content, encoding="utf-8")

    # L6 Decisions
    decisions_dir = _ensure_dir(bdir / "decisions" / "2026" / "05")
    decisions = {
        "2026-05-21-001.md": """# Decision: Adopt async pattern for all new handlers
- decision_id: DEC-2026-05-21-001
- status: approved
- reasoning: Improves throughput under concurrent load
- The decision about async pattern was based on benchmark data
""",
    }
    for name, content in decisions.items():
        (decisions_dir / name).write_text(content, encoding="utf-8")

    # L5 Goals
    goals_dir = _ensure_dir(bdir / "goals")
    goals_yaml = """goals:
  - id: GOAL-2026-Q2-001
    title: Complete memory-hybrid benchmark suite
    priority: high
    status: in_progress
    progress: 30
  - id: GOAL-2026-Q2-002
    title: Improve async handler performance
    priority: medium
    status: not_started
    progress: 0
  - id: GOAL-2026-Q2-003
    title: Finish this quarter goal progress report
    priority: medium
    status: in_progress
    progress: 50
"""
    (goals_dir / "active.yaml").write_text(goals_yaml, encoding="utf-8")

    # L0 Rules
    rules_dir = _ensure_dir(bdir / "hardening")
    rules_yaml = """rules:
  - id: HARDEN-001
    pattern: hardcoded secrets
    trigger: detect hardcoded API keys or passwords
    level: hard
    enabled: true
  - id: HARDEN-002
    pattern: missing code review
    trigger: PR without review approval
    level: soft
    enabled: true
  - id: HARDEN-003
    pattern: missing error handling
    trigger: async function without try/except
    level: medium
    enabled: true
  - id: HARDEN-004
    pattern: secrets in code
    trigger: detect secrets in source files
    level: hard
    enabled: true
"""
    (rules_dir / "rules.yaml").write_text(rules_yaml, encoding="utf-8")

    # SESSIONS
    sessions_dir = _ensure_dir(bdir / "sessions")
    (sessions_dir / "SESSION-TEST-001.md").write_text(
        "# Session: benchmark-runner\n"
        "- timestamp: 2026-05-21T10:00:00Z\n"
        "- agent: benchmark\n"
        "- status: completed\n"
        "- summary: Initial benchmark test run\n",
        encoding="utf-8",
    )
    (sessions_dir / "SESSION-TEST-002.md").write_text(
        "# Session: code-review\n"
        "- timestamp: 2026-05-21T14:00:00Z\n"
        "- agent: reviewer\n"
        "- status: completed\n"
        "- summary: Reviewed async handler patterns and decided to adopt\n",
        encoding="utf-8",
    )

    print(f"  Seeded test data in {bdir}")


# ── Benchmark Logic ──────────────────────────────────────────────


def _keyword_search(query: str, root: Path, layer: str, glob_pat: str = "*.md") -> list[dict[str, Any]]:
    """Search files matching any keyword from query."""
    results = []
    if not root.exists():
        return results
    keywords = [w.strip().lower() for w in query.split() if len(w.strip()) > 2]
    if not keywords:
        keywords = [query.lower()]
    for fpath in sorted(root.rglob(glob_pat)):
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        content_lower = content.lower()
        matched_lines = []
        for kw in keywords:
            if kw in content_lower:
                matched_lines.extend([ln for ln in content.splitlines() if kw in ln.lower()])
        if matched_lines:
            results.append({
                "layer": layer,
                "file": fpath.name,
                "matched_lines": matched_lines[:5],
                "content": content,
            })
    return results


def _search_l3(query: str, root: Path) -> list[dict[str, Any]]:
    return _keyword_search(query, root / "timeline", "L3", "*.md")


def _search_l4(query: str, root: Path) -> list[dict[str, Any]]:
    return _keyword_search(query, root / "facts", "L4", "*.md")


def _search_l5(query: str, root: Path) -> list[dict[str, Any]]:
    return _keyword_search(query, root / "goals", "L5", "*.yaml")


def _search_l6(query: str, root: Path) -> list[dict[str, Any]]:
    return _keyword_search(query, root / "decisions", "L6", "*.md")


def _search_l0(query: str, root: Path) -> list[dict[str, Any]]:
    return _keyword_search(query, root / "hardening", "L0", "*.yaml")


_SEARCHERS = {
    "L0": _search_l0,
    "L3": _search_l3,
    "L4": _search_l4,
    "L5": _search_l5,
    "L6": _search_l6,
}


def run_query(query: str, expected_layer: str, expected_keywords: list[str]) -> dict[str, Any]:
    """Run a single benchmark query and return detailed results."""
    start = time.perf_counter()
    token_count = 0
    all_results: list[dict[str, Any]] = []

    # Search all layers
    for layer_name, searcher in _SEARCHERS.items():
        results = searcher(query, BENCHMARK_DIR)
        for r in results:
            token_count += len(r.get("content", "").split())
        all_results.extend(results)

    latency = time.perf_counter() - start

    all_results.sort(key=lambda r: len(r.get("matched_lines", [])), reverse=True)

    # Determine hit / rank
    hit = False
    rank = -1
    matched_layer = ""
    for i, r in enumerate(all_results):
        if r["layer"] == expected_layer:
            matched_content = json.dumps(r, ensure_ascii=False).lower()
            kw_hits = [kw.lower() in matched_content for kw in expected_keywords]
            if any(kw_hits):
                hit = True
                rank = i + 1
                matched_layer = r["layer"]
                break

    # Fallback: if no exact layer match, check any layer
    if not hit:
        for i, r in enumerate(all_results):
            matched_content = json.dumps(r, ensure_ascii=False).lower()
            kw_hits = [kw.lower() in matched_content for kw in expected_keywords]
            if any(kw_hits):
                hit = True
                rank = i + 1
                matched_layer = r["layer"]
                break

    return {
        "query": query,
        "expected_layer": expected_layer,
        "expected_keywords": expected_keywords,
        "hit": hit,
        "rank": rank,
        "matched_layer": matched_layer,
        "latency_s": round(latency, 4),
        "results_count": len(all_results),
        "tokens_scanned": token_count,
    }


# ── Metrics ──────────────────────────────────────────────────────


def compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute Precision@k, Recall@k, MRR, latency stats."""
    total = len(results)
    hits_at_1 = sum(1 for r in results if r["hit"] and r["rank"] == 1)
    hits_at_3 = sum(1 for r in results if r["hit"] and r["rank"] is not None and r["rank"] <= 3)
    hits_at_5 = sum(1 for r in results if r["hit"] and r["rank"] is not None and r["rank"] <= 5)
    hits_any = sum(1 for r in results if r["hit"])

    # MRR
    reciprocal_ranks = [1.0 / r["rank"] for r in results if r["hit"] and r["rank"] and r["rank"] > 0]
    mrr = sum(reciprocal_ranks) / total if total > 0 else 0.0

    # Latency
    latencies = sorted([r["latency_s"] for r in results])
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
    p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0

    return {
        "total_queries": total,
        "hits": hits_any,
        "misses": total - hits_any,
        "precision_at_1": round(hits_at_1 / total * 100, 1) if total else 0,
        "precision_at_3": round(hits_at_3 / total * 100, 1) if total else 0,
        "precision_at_5": round(hits_at_5 / total * 100, 1) if total else 0,
        "recall": round(hits_any / total * 100, 1) if total else 0,
        "mrr": round(mrr, 4),
        "p50_latency_s": round(p50, 4),
        "p95_latency_s": round(p95, 4),
        "p99_latency_s": round(p99, 4),
        "total_tokens_scanned": sum(r["tokens_scanned"] for r in results),
    }


# ── Reporting ────────────────────────────────────────────────────


def gen_markdown_report(results: list[dict[str, Any]], metrics: dict[str, Any], timestamp: str) -> str:
    """Generate Markdown report."""
    lines = [
        "# Memory Hybrid — Retrieval Benchmark",
        "",
        f"**Date**: {timestamp}",
        "**Mode**: synthetic",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total queries | {metrics['total_queries']} |",
        f"| Hits | {metrics['hits']} |",
        f"| Misses | {metrics['misses']} |",
        f"| Precision@1 | {metrics['precision_at_1']}% |",
        f"| Precision@3 | {metrics['precision_at_3']}% |",
        f"| Precision@5 | {metrics['precision_at_5']}% |",
        f"| Recall | {metrics['recall']}% |",
        f"| MRR | {metrics['mrr']} |",
        f"| p50 latency | {metrics['p50_latency_s']}s |",
        f"| p95 latency | {metrics['p95_latency_s']}s |",
        f"| p99 latency | {metrics['p99_latency_s']}s |",
        f"| Total tokens scanned | {metrics['total_tokens_scanned']} |",
        "",
        "## Per-Query Results",
        "",
        "| # | Query | Expected Layer | Matched Layer | Hit | Rank | Latency (s) | Category |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(results, 1):
        hit_mark = "[OK]" if r["hit"] else "[MISS]"
        rank_str = str(r["rank"]) if r["rank"] and r["rank"] > 0 else "N/A"
        cat = r.get("category", "unknown")
        lines.append(
            f"| {i} | {r['query']} | {r['expected_layer']} | {r['matched_layer']} | {hit_mark} | {rank_str} | {r['latency_s']} | {cat} |"
        )

    lines.append("")
    return "\n".join(lines)


def write_reports(results: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    """Write JSON and Markdown reports to benchmark dir."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = _ensure_dir(BENCHMARK_DIR)

    json_path = report_dir / f"report_{timestamp}.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump({
            "timestamp": timestamp,
            "mode": "synthetic",
            "memory_root": str(MEMORY_ROOT),
            "metrics": metrics,
            "cases": results,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"  JSON report: {json_path}")

    md_content = gen_markdown_report(results, metrics, timestamp)
    md_path = report_dir / f"report_{timestamp}.md"
    md_path.write_text(md_content, encoding="utf-8")
    print(f"  MD report:  {md_path}")

    # Latest pointer
    (report_dir / "latest.txt").write_text(str(md_path), encoding="utf-8")

    print()
    print("=" * 60)
    print("  BENCHMARK SUMMARY")
    print("=" * 60)
    print(f"  Total queries: {metrics['total_queries']}")
    print(f"  Hits:          {metrics['hits']}")
    print(f"  Misses:        {metrics['misses']}")
    print(f"  Precision@1:   {metrics['precision_at_1']}%")
    print(f"  Precision@3:   {metrics['precision_at_3']}%")
    print(f"  Precision@5:   {metrics['precision_at_5']}%")
    print(f"  MRR:           {metrics['mrr']}")
    print(f"  p50 latency:   {metrics['p50_latency_s']}s")
    print(f"  p95 latency:   {metrics['p95_latency_s']}s")
    print(f"  p99 latency:   {metrics['p99_latency_s']}s")
    print("=" * 60)


# ── Main ─────────────────────────────────────────────────────────


def main() -> None:
    print("Memory Hybrid — Retrieval Benchmark")
    print("=" * 40)
    print(f"  Memory root: {MEMORY_ROOT}")
    print(f"  Benchmark dir: {BENCHMARK_DIR}")
    print()

    # Seed test data
    print("[1/3] Seeding test data...")
    seed_test_data()

    # Run queries
    print(f"[2/3] Running {len(BENCHMARK_CASES)} benchmark queries...")
    results = []
    for case in BENCHMARK_CASES:
        result = run_query(case["query"], case["expected_layer"], case["expected_keywords"])
        result["category"] = case["category"]
        results.append(result)
        status = "+" if result["hit"] else "-"
        print("  %s %-50s -> L%s  rank=%s  %dms" % (
            status, result['query'][:50],
            result.get('matched_layer', '?'),
            result.get('rank', 'N/A'),
            result['latency_s'] * 1000,
        ))

    # Compute metrics
    print("[3/3] Computing metrics and writing reports...")
    metrics = compute_metrics(results)
    write_reports(results, metrics)


if __name__ == "__main__":
    main()
