# Enrichment Daemon — Phase 2 Design

**Date**: 2026-05-29
**Project**: memory-hybrid
**Status**: Approved

## Overview

Add enrichment capabilities to the SQLite-backed memory system: age-based decay
scoring (+ archival) and vector-similarity deduplication (+ merge). Both are
invoked as MCP tools — no cron, no background threads.

## Architecture

```
MCP tool (run_enrichment)
    ├── DecayEngine.run(dry_run)
    │   → UPDATE memory_items SET decay_score, archived=1
    └── DedupEngine.run(threshold, dry_run)
        → DELETE duplicate memory_items (FTS+vec cleaned by triggers)
```

Both engines live under `backend/enrichment/` as library classes. The MCP server
loads them via `_store_singleton()`.

## 1. Schema Changes (SCHEMA_VERSION 1 → 2)

```sql
ALTER TABLE memory_items ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0;
```

Migration in `SQLiteStore._init_db()`:

```python
if current_version < 2:
    conn.execute("ALTER TABLE memory_items ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE memory_items SET access_count = 0 WHERE access_count IS NULL")
```

Backward compatible. No new tables.

## 2. access_count — Two Trigger Points

### Auto-increment in `recall()`

After query results are fetched, batch-update all returned IDs:

```python
ids = [r["id"] for r in results]
placeholders = ",".join("?" for _ in ids)
conn.execute(
    f"UPDATE memory_items SET access_count = access_count + 1 WHERE id IN ({placeholders})",
    ids,
)
```

### MCP tool `mark_accessed(memory_id: str)`

Explicit single-ID increment for external access tracking.

## 3. DecayEngine

**File**: `backend/enrichment/nightly_decay.py`

### Algorithm

```
decay_score = ln(1 + age_hours) * layer_weight - access_count * 0.01
```

| Layer | Weight | Rationale |
|-------|--------|-----------|
| L3 (temporal) | 0.3 | Fastest decay — daily logs become irrelevant |
| L4 (facts) | 0.2 | Facts age slower |
| L5 (goals) | 0.1 | Slowest decay — goals persist |
| L6 (decisions) | 0.15 | Decisions age moderately |

**Threshold**: `decay_score > 5.0` → `archived = 1`
**access_count term**: `- access_count * 0.01` — frequently accessed memories
survive longer.

### Behavior

- Scans all non-archived `memory_items`, ordered by `created_at ASC`
- Computes decay, updates `decay_score` and optionally sets `archived=1`
- Dry-run: computes everything, logs, returns stats without mutation
- No new dependencies (uses `math.log`, `datetime`)

### Return

```json
{
  "scanned": 150,
  "decayed": 150,
  "archived": 12,
  "dry_run": true,
  "threshold": 5.0
}
```

## 4. DedupEngine

**File**: `backend/enrichment/dedup.py`

### Algorithm

1. Query non-archived memory items (with embeddings)
2. For each, call `_recall_vector(content, top_k=5)` to find nearest neighbors
3. Filter: `distance < (1 - threshold)` AND same layer AND different ID
4. Pair as (keeper, duplicate) — keeper has lower `decay_score` (more valuable)
5. Merge: keeper metadata gets `{"duplicate_merged": true, "merged_ids": [...]}`
6. Delete duplicate: memory_vec entry first, then memory_items (+ FTS trigger)

### Threshold

Default `0.92` cosine similarity (= `distance < 0.08`). Configurable per run.

### Safety

- Skips items without embeddings (fallback to FTS-only)
- Dry-run: lists all candidate pairs without mutation
- Delete order: `memory_vec` → `memory_items` (FTS trigger auto-cleans)

### Return

```json
{
  "pairs_found": 8,
  "merged": 5,
  "skipped": 3,
  "dry_run": true,
  "threshold": 0.92
}
```

## 5. MCP Tool Integration

Three new tools on `mcp_server.py`:

### `run_decay(dry_run: bool = True)`
Run DecayEngine. Returns stats. Dry-run by default.

### `run_dedup(threshold: float = 0.92, dry_run: bool = True)`
Run DedupEngine. Returns stats. Dry-run by default.

### `run_enrichment(enrichment_type: str = "all", dry_run: bool = True)`
Run one or both: `"decay"`, `"dedup"`, `"all"`. Dry-run by default. Runs decay
before dedup when `"all"` (reduces noise for dedup).

### `mark_accessed(memory_id: str)`
Increment access_count by 1 for a specific memory.

## 6. File Structure

```
backend/enrichment/
  __init__.py       # exports DecayEngine, DedupEngine
  nightly_decay.py  # DecayEngine class
  dedup.py          # DedupEngine class
```

## 7. Non-Goals (Out of Scope)

- No cron/scheduler — enrichment is agent-triggered via MCP tools
- No automatic decay — agent must explicitly call `run_enrichment`
- No background thread in MCP server
- No access log/history tracking beyond `access_count`
- No cross-layer dedup (only same-layer pairs matched)

## 8. Migration Path

- Schema v1 → v2: `ALTER TABLE` + backfill
- Existing data: untouched until enrichment is first called
- Downgrade: no loss — `access_count` column is additive
