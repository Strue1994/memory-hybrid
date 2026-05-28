"""Memory System API — Hybrid Backend.

Combines Qdrant (vector) + Neo4j (graph) + file cache.
Memory Hybrid V2 adds lifecycle metadata, task-aware recall, and
L6-to-L0 hardening candidate promotion.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import zipfile
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from .config import settings
from .lifecycle import build_lifecycle
from .models import LifecycleState, TaskMode, TemporalMode
from .qdrant_client import QdrantClient
from .neo4j_client import Neo4jClient
from .router import classify_query, classify_temporal_intent
from .promotion import decision_to_candidate
from .review_flow import (
    approve_candidate,
    unified_timeline,
    filter_candidate_reviews,
    filter_review_log,
    ensure_memory_root,
    candidate_rule_links,
    list_rules,
    list_candidate_reviews,
    diff_rule_snapshots,
    list_rule_snapshots,
    preferred_memory_root,
    read_review_log,
    reject_candidate,
    rollback_rules,
    set_rule_enabled,
    sort_candidates,
    slice_items,
    write_candidate_review,
)
from .scoring import dedupe_ranked, score_item, score_temporal


# ── Globals (initialized in lifespan) ────────────────────────────

qdrant: QdrantClient | None = None
neo4j: Neo4jClient | None = None


# ── Request / Response models ────────────────────────────────────

class SaveMemoryRequest(BaseModel):
    content: str
    layer: str  # "L3" | "L4" | "L5"
    metadata: dict[str, Any] = {}


class RecallRequest(BaseModel):
    query: str
    layers: list[str] | None = None  # default: auto-classify -> all
    top_k: int = 5
    filters: dict[str, Any] = {}


class SaveDecisionRequest(BaseModel):
    content: str
    decision_id: str
    metadata: dict[str, Any] = {}


class MemoryItem(BaseModel):
    id: str
    score: float
    layer: str
    content: str
    metadata: dict[str, Any]


# ── Lifespan ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global qdrant, neo4j
    try:
        qdrant = QdrantClient(settings.qdrant_url)
        await qdrant.ping()
    except Exception as e:
        print(f"[memory-api] Qdrant unavailable: {e}")
        qdrant = None

    try:
        neo4j = Neo4jClient(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        await neo4j.ping()
        await neo4j.create_constraints()
    except Exception as e:
        print(f"[memory-api] Neo4j unavailable: {e}")
        neo4j = None

    if qdrant is None and neo4j is None:
        print("[memory-api] WARNING: Both Qdrant and Neo4j unavailable — API will return errors")

    yield

    if qdrant:
        await qdrant.close()
    if neo4j:
        await neo4j.close()


app = FastAPI(
    title="Memory System Hybrid API",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Health ───────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "qdrant": qdrant is not None,
        "neo4j": neo4j is not None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── L3: Temporal Memory ──────────────────────────────────────────

@app.post("/v1/layers/L3/save")
async def save_temporal(req: SaveMemoryRequest):
    if qdrant is None:
        raise HTTPException(503, "Qdrant unavailable")
    point_id = await qdrant.upsert(
        collection="temporal_memory",
        content=req.content,
        payload={
            "layer": "L3",
            "date": req.metadata.get("date", datetime.now(timezone.utc).date().isoformat()),
            "project": req.metadata.get("project", ""),
            "event_type": req.metadata.get("event_type", "work"),
            "tags": req.metadata.get("tags", []),
            "file_path": req.metadata.get("file_path", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "lifecycle": req.metadata.get("lifecycle") or build_lifecycle("L3", LifecycleState.CAPTURED),
        },
    )
    return {"id": point_id, "layer": "L3"}


@app.get("/v1/layers/L3/recall")
async def recall_temporal(
    query: str = Query(...),
    top_k: int = Query(5),
    date_from: str | None = None,
    date_to: str | None = None,
    project: str | None = None,
    event_type: str | None = None,
    tags: str | None = None,
    # Phase 31: temporal scoring
    temporal_mode: TemporalMode = Query(TemporalMode.AUTO),
):
    if qdrant is None:
        raise HTTPException(503, "Qdrant unavailable")
    filters = {"must": []}
    if date_from:
        filters["must"].append({"key": "date", "range": {"gte": date_from}})
    if date_to:
        filters["must"].append({"key": "date", "range": {"lte": date_to}})
    if project:
        filters["must"].append({"key": "project", "match": {"value": project}})
    if event_type:
        filters["must"].append({"key": "event_type", "match": {"value": event_type}})
    if tags:
        filters["must"].append({"key": "tags", "match": {"any": [t.strip() for t in tags.split(",")]}})

    if not filters["must"]:
        filters = None

    results = await qdrant.search("temporal_memory", query, top_k, query_filter=filters)
    items = [MemoryItem(**r) for r in results]

    # Apply temporal scoring
    resolved_mode = classify_temporal_intent(query) if temporal_mode == TemporalMode.AUTO else temporal_mode
    for item in items:
        created_value = item.metadata.get("created_at", "")
        created_str = created_value if isinstance(created_value, str) else ""
        item.score = score_temporal(created_str, mode=resolved_mode, base_score=item.score)

    items.sort(key=lambda x: x.score, reverse=True)
    return items[:top_k]


# ── L4: Fact Curation ────────────────────────────────────────────

@app.post("/v1/layers/L4/save")
async def save_fact(req: SaveMemoryRequest):
    if qdrant is None:
        raise HTTPException(503, "Qdrant unavailable")
    point_id = await qdrant.upsert(
        collection="semantic_memory",
        content=req.content,
        payload={
            "layer": "L4",
            "category": req.metadata.get("category", "general"),
            "importance": req.metadata.get("importance", 3),
            "tags": req.metadata.get("tags", []),
            "source": req.metadata.get("source", "manual"),
            "strike_count": req.metadata.get("strike_count", 0),
            "verified": req.metadata.get("verified", False),
            "file_path": req.metadata.get("file_path", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "lifecycle": req.metadata.get("lifecycle") or build_lifecycle("L4", LifecycleState.PENDING),
        },
    )
    return {"id": point_id, "layer": "L4"}


@app.get("/v1/layers/L4/recall")
async def recall_facts(
    query: str = Query(...),
    top_k: int = Query(5),
    category: str | None = None,
    min_importance: int | None = None,
    tags: str | None = None,
    verified_only: bool = Query(True),
):
    if qdrant is None:
        raise HTTPException(503, "Qdrant unavailable")
    filters = {"must": []}
    if category:
        filters["must"].append({"key": "category", "match": {"value": category}})
    if min_importance is not None:
        filters["must"].append({"key": "importance", "range": {"gte": min_importance}})
    if tags:
        filters["must"].append({"key": "tags", "match": {"any": [t.strip() for t in tags.split(",")]}})
    if verified_only:
        filters["must"].append({"key": "verified", "match": {"value": True}})

    if not filters["must"]:
        filters = None

    results = await qdrant.search("semantic_memory", query, top_k, query_filter=filters)
    return [MemoryItem(**r) for r in results]


# ── L5: Digital Self / Goals ─────────────────────────────────────

@app.post("/v1/layers/L5/save")
async def save_goal(req: SaveMemoryRequest):
    if qdrant is None:
        raise HTTPException(503, "Qdrant unavailable")
    point_id = await qdrant.upsert(
        collection="aspiration_memory",
        content=req.content,
        payload={
            "layer": "L5",
            "goal_id": req.metadata.get("goal_id", ""),
            "title": req.metadata.get("title", ""),
            "priority": req.metadata.get("priority", "medium"),
            "status": req.metadata.get("status", "not_started"),
            "progress": req.metadata.get("progress", 0),
            "deadline": req.metadata.get("deadline", ""),
            "related_skills": req.metadata.get("related_skills", []),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "lifecycle": req.metadata.get("lifecycle") or build_lifecycle("L5", LifecycleState.CURATED),
        },
    )
    return {"id": point_id, "layer": "L5"}


@app.get("/v1/layers/L5/recall")
async def recall_goals(
    query: str = Query(...),
    top_k: int = Query(5),
    status: str | None = None,
    priority: str | None = None,
):
    if qdrant is None:
        raise HTTPException(503, "Qdrant unavailable")
    filters = {"must": []}
    if status:
        filters["must"].append({"key": "status", "match": {"value": status}})
    if priority:
        filters["must"].append({"key": "priority", "match": {"value": priority}})

    if not filters["must"]:
        filters = None

    results = await qdrant.search("aspiration_memory", query, top_k, query_filter=filters)
    return [MemoryItem(**r) for r in results]


# ── L2: Persona / Relationship Graph ─────────────────────────────

class SaveRelationRequest(BaseModel):
    content: str
    people: list[str] = []
    skills: list[str] = []
    source: str = "manual"


@app.post("/v1/layers/L2/save")
async def save_relationship(req: SaveRelationRequest):
    if neo4j is None:
        raise HTTPException(503, "Neo4j unavailable")
    memory_id = await neo4j.save_memory(
        content=req.content,
        people=req.people,
        skills=req.skills,
        source=req.source,
    )
    return {"id": memory_id, "layer": "L2"}


@app.get("/v1/layers/L2/recall")
async def recall_relationships(
    query: str = Query(...),
    top_k: int = Query(5),
):
    if neo4j is None:
        raise HTTPException(503, "Neo4j unavailable")
    results = await neo4j.search_memory(query_text=query, top_k=top_k)
    return [MemoryItem(**r) for r in results]


@app.get("/v1/layers/L2/graph/stats")
async def graph_stats():
    if neo4j is None:
        raise HTTPException(503, "Neo4j unavailable")
    return await neo4j.get_stats()


# ── L6: Decision Audit / Promotion ───────────────────────────────

@app.post("/v1/layers/L6/save")
async def save_decision(req: SaveDecisionRequest):
    metadata = {
        **req.metadata,
        "layer": "L6",
        "decision_id": req.decision_id,
        "created_at": req.metadata.get("created_at", datetime.now(timezone.utc).isoformat()),
        "lifecycle": req.metadata.get("lifecycle") or build_lifecycle("L6", LifecycleState.VERIFIED),
    }
    return {
        "id": req.decision_id,
        "layer": "L6",
        "content": req.content,
        "metadata": metadata,
    }


@app.post("/v1/layers/L6/promote")
async def promote_decision(req: SaveDecisionRequest):
    candidate = decision_to_candidate(req.decision_id, req.content, req.metadata)
    return candidate.model_dump()


@app.post("/v1/layers/L0/candidates/from-decision")
async def materialize_hardening_candidate(req: SaveDecisionRequest):
    candidate = decision_to_candidate(req.decision_id, req.content, req.metadata)
    review_path = write_candidate_review(candidate)
    payload = candidate.model_dump()
    payload["review_path"] = str(review_path)
    payload["status"] = "pending"
    return payload


@app.post("/v1/layers/L0/candidates/{candidate_id}/approve")
async def approve_hardening_candidate(candidate_id: str):
    return approve_candidate(candidate_id)


@app.post("/v1/layers/L0/candidates/{candidate_id}/reject")
async def reject_hardening_candidate(candidate_id: str):
    return reject_candidate(candidate_id)


@app.post("/v1/layers/L0/rules/rollback")
async def rollback_hardening_rules():
    return rollback_rules()


@app.post("/v1/layers/L0/rules/{rule_id}/enable")
async def enable_hardening_rule(rule_id: str):
    return set_rule_enabled(rule_id, True)


@app.post("/v1/layers/L0/rules/{rule_id}/disable")
async def disable_hardening_rule(rule_id: str, reason: str | None = None):
    return set_rule_enabled(rule_id, False, reason=reason)


@app.get("/v1/layers/L0/candidates")
async def list_hardening_candidates(
    status: str | None = None,
    candidate_id: str | None = None,
    q: str | None = None,
    sort_by_priority: bool = False,
    offset: int = 0,
    limit: int | None = None,
    recent: int | None = None,
):
    items = filter_candidate_reviews(status=status, candidate_id=candidate_id)
    if q:
        q_lower = q.lower()
        items = [item for item in items if q_lower in str(item).lower()]
    items = sort_candidates(items, by_priority=sort_by_priority)
    sliced = slice_items(items, offset=offset, limit=limit, recent=recent)
    return {"count": len(items), "items": sliced}


@app.get("/v1/layers/L0/review-log")
async def get_hardening_review_log(
    candidate_id: str | None = None,
    action: str | None = None,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    offset: int = 0,
    limit: int | None = None,
    recent: int | None = None,
):
    payload = filter_review_log(candidate_id=candidate_id, action=action, since=since, until=until, q=q)
    events = payload.get("events", [])
    return {"count": len(events), "events": slice_items(events, offset=offset, limit=limit, recent=recent)}


@app.get("/v1/layers/L0/rules/history")
async def get_hardening_rules_history(
    since: str | None = None,
    until: str | None = None,
    offset: int = 0,
    limit: int | None = None,
    recent: int | None = None,
):
    snapshots = [{"path": path} for path in list_rule_snapshots(since=since, until=until)]
    return {"count": len(snapshots), "snapshots": slice_items(snapshots, offset=offset, limit=limit, recent=recent)}


@app.get("/v1/layers/L0/rules/history/diff")
async def get_hardening_rules_history_diff(snapshot_a: str | None = None, snapshot_b: str | None = None):
    return diff_rule_snapshots(snapshot_a=snapshot_a, snapshot_b=snapshot_b)


@app.get("/v1/layers/L0/candidate-rule-links")
async def get_candidate_rule_links():
    links = candidate_rule_links()
    return {"count": len(links), "links": links}


@app.get("/v1/layers/L0/rules")
async def get_hardening_rules(enabled: bool | None = None, q: str | None = None):
    rules = list_rules(enabled=enabled, q=q)
    return {"count": len(rules), "rules": rules}


@app.get("/v1/layers/L0/timeline")
async def get_hardening_timeline(
    kind: str | None = None,
    candidate_id: str | None = None,
    rule_id: str | None = None,
    severity: str | None = None,
    since: str | None = None,
    until: str | None = None,
    recent: int | None = None,
    # Phase 20-24: enhanced filtering
    search: str | None = None,
    status: str | None = None,
    level: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
):
    items = unified_timeline()
    if kind:
        items = [item for item in items if item.get("kind") == kind]
    if candidate_id:
        items = [item for item in items if item.get("id") == candidate_id or item.get("payload", {}).get("candidate_id") == candidate_id]
    if rule_id:
        items = [item for item in items if item.get("id") == rule_id or item.get("payload", {}).get("rule_id") == rule_id]
    if severity:
        sev = severity.lower()
        items = [item for item in items if str(item.get("payload", {}).get("severity", "")).lower() == sev]
    # Phase 20: text search across id, payload fields, timestamp
    if search:
        q = search.lower()
        items = [
            item for item in items
            if q in str(item.get("id", "")).lower()
            or q in str(item.get("payload", {})).lower()
            or q in str(item.get("timestamp", "")).lower()
        ]
    # Phase 20: status filter (applies to candidate/review_event payload.status)
    if status:
        st = status.lower()
        items = [item for item in items if str(item.get("payload", {}).get("status", "")).lower() == st]
    # Phase 20: level filter (applies to candidate payload.level)
    if level:
        lv = level.lower()
        items = [item for item in items if str(item.get("payload", {}).get("level", "")).lower() == lv]
    if since or until:
        filtered = []
        for item in items:
            ts = str(item.get("timestamp", ""))
            if not ts:
                continue
            from .review_flow import _within_range
            if _within_range(ts, since=since, until=until):
                filtered.append(item)
        items = filtered
    total_count = len(items)
    if recent is not None:
        items = items[:recent]
    # Phase 24: pagination (after recent slicing)
    if offset is not None:
        offset = max(0, offset)
        items = items[offset:]
    if limit is not None:
        limit = max(1, limit)
        items = items[:limit]
    stats = {
        "filtered": total_count,
        "candidate": sum(1 for item in items if item.get("kind") == "candidate"),
        "review_event": sum(1 for item in items if item.get("kind") == "review_event"),
        "rule": sum(1 for item in items if item.get("kind") == "rule"),
    }
    return {"count": len(items), "total": total_count, "stats": stats, "items": items}



@app.get("/v1/layers/L0/timeline/export/batch")
async def batch_export_hardening_timeline(view_kinds: str | None = None, export_format: str = "html", zip_bundle: bool = False):
    kinds = [kind.strip() for kind in view_kinds.split(",")] if view_kinds else ["candidate", "review_event", "rule"]
    views = [
        {"kind": kind} for kind in kinds
    ]
    outputs = []
    for view in views:
        outputs.append(await export_hardening_timeline(kind=view["kind"], export_format=export_format))
    if zip_bundle:
        from pathlib import Path

        export_dir = Path(_timeline_export_dir())
        zip_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        zip_path = export_dir / f"timeline-batch-{zip_ts}.zip"
        total_size = 0
        with zipfile.ZipFile(zip_path, "w") as zf:
            for item in outputs:
                zf.write(item["path"], arcname=Path(item["path"]).name)
                total_size += item.get("file_size", 0)
            manifest = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(outputs),
                "total_size_hr": _human_size(total_size),
                "exports": [
                    {
                        "filename": Path(item["path"]).name,
                        "format": item.get("format", ""),
                        "file_size": item.get("file_size", 0),
                        "file_size_hr": item.get("file_size_hr", ""),
                        "written_at": item.get("written_at", ""),
                        "filter_kind": item.get("exports", [{}])[0].get("kind") if isinstance(item.get("exports"), list) and len(item.get("exports")) > 0 else None,
                    }
                    for item in outputs
                ],
            }
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            readme_lines = [
                "═══════════════════════════════════════════════",
                "  Memory Hybrid Timeline Batch Export",
                "═══════════════════════════════════════════════",
                "",
                f"Generated: {manifest['generated_at']}",
                f"View count: {manifest['count']}",
                f"Total size: {manifest['total_size_hr']}",
                "",
                "┌ Contents ───────────────────────────────────",
            ]
            for idx, exp in enumerate(manifest["exports"]):
                readme_lines.append(f"│ {idx+1}. {exp['filename']}  ({exp['file_size_hr']}, {exp['format']})")
            readme_lines.append("└────────────────────────────────────────────")
            readme_lines.append("")
            readme_lines.append("Files:")
            for exp in manifest["exports"]:
                readme_lines.append(f"  - {exp['filename']} ({exp['file_size_hr']})")
            readme_lines.append("")
            zf.writestr("README.txt", "\n".join(readme_lines))
            # index.html: proper dashboard with cards
            index_items = "".join(
                f"""
                <div class='card'>
                    <h3>{Path(item['path']).name}</h3>
                    <div class='meta-row'><span class='label'>Format</span><span>{item.get('format','')}</span></div>
                    <div class='meta-row'><span class='label'>Size</span><span>{item.get('file_size_hr','')}</span></div>
                    <div class='meta-row'><span class='label'>Written</span><span>{item.get('written_at','')}</span></div>
                    <div class='meta-row'><span class='label'>Kind filter</span><span>{next((f[1] for f in [item.get('filter_kind','')] if f), 'all')}</span></div>
                    <a class='download-btn' href='{Path(item['path']).name}'>Open</a>
                </div>"""
                for item in outputs
            )
            index_html = f"""<html><head><meta charset='utf-8'><title>Timeline Batch Export</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:Segoe UI,Arial,sans-serif;padding:32px;color:#222;background:#f6f8fa}}
h1{{margin-bottom:8px}}
.subtitle{{color:#656d76;margin-bottom:24px}}
.dashboard{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}}
.card{{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:16px}}
.card h3{{font-size:14px;margin-bottom:12px;word-break:break-all}}
.meta-row{{display:flex;justify-content:space-between;padding:4px 0;font-size:13px;border-bottom:1px solid #f0f0f0}}
.meta-row .label{{color:#656d76}}
.download-btn{{display:inline-block;margin-top:12px;padding:6px 16px;background:#2da44e;color:#fff;border-radius:6px;text-decoration:none;font-size:13px;font-weight:500}}
.download-btn:hover{{background:#218838}}
.summary-bar{{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:16px;margin-bottom:24px;display:flex;gap:32px}}
.summary-item{{text-align:center}}
.summary-item .num{{font-size:24px;font-weight:600}}
.summary-item .lbl{{font-size:12px;color:#656d76}}
</style></head><body>
<h1>Timeline Batch Export</h1>
<p class='subtitle'>Generated {manifest['generated_at']}</p>
<div class='summary-bar'>
    <div class='summary-item'><div class='num'>{manifest['count']}</div><div class='lbl'>Views</div></div>
    <div class='summary-item'><div class='num'>{manifest['total_size_hr']}</div><div class='lbl'>Total Size</div></div>
    <div class='summary-item'><div class='num'>{export_format}</div><div class='lbl'>Format</div></div>
</div>
<div class='dashboard'>{index_items}</div>
</body></html>"""
            zf.writestr("index.html", index_html)
        return {"count": len(outputs), "exports": outputs, "zip_path": str(zip_path), "total_size_hr": _human_size(total_size)}
    return {"count": len(outputs), "exports": outputs}


@app.get("/v1/layers/L0/timeline/export/overview")
async def export_hardening_overview(severity: str | None = None, since: str | None = None, until: str | None = None):
    """Combined overview: fetches all timeline kinds and renders a single-page HTML dashboard."""
    from pathlib import Path
    kinds = ["candidate", "review_event", "rule"]
    kind_results = {}
    total_items = 0
    severity_counts: dict[str, int] = {}
    day_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    for kind in kinds:
        payload = await get_hardening_timeline(kind=kind, severity=severity, since=since, until=until)
        kind_counts[kind] = payload["count"]
        total_items += payload["count"]
        for item in payload["items"]:
            sev = str(item.get("payload", {}).get("severity", "none")).lower()
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            ts = item.get("timestamp", "")
            if ts:
                day = ts[:10]  # YYYY-MM-DD
                day_counts[day] = day_counts.get(day, 0) + 1
        kind_results[kind] = payload["items"]
    sorted_days = sorted(day_counts.keys())
    max_day_count = max(day_counts.values()) if day_counts else 1
    bar_chart = "".join(
        f"<div style='display:flex;align-items:center;margin:2px 0'><span style='width:100px;font-size:11px;color:#656d76'>{d}</span><div style='height:20px;width:{max(8, day_counts[d]/max_day_count*200)}px;background:#2da44e;border-radius:4px;display:flex;align-items:center;justify-content:flex-end;padding-right:4px'><span style='color:#fff;font-size:11px'>{day_counts[d]}</span></div></div>"
        for d in sorted_days[-30:]  # last 30 days
    )
    severity_colors = {"high": "#ffebe9", "medium": "#fff8c5", "low": "#dafbe1", "none": "#f6f8fa"}
    sev_bars = "".join(
        f"<div style='display:flex;align-items:center;margin:2px 0'><span style='width:80px;font-size:11px;color:#656d76'>{sev}</span><div style='height:20px;width:{max(8, cnt/max(severity_counts.values() or [1])*200)}px;background:{severity_colors.get(sev,"#d0d7de")};border:1px solid #d0d7de;border-radius:4px;display:flex;align-items:center;padding:0 4px'><span style='font-size:11px'>{cnt}</span></div></div>"
        for sev, cnt in sorted(severity_counts.items())
    )
    html = f"""<html><head><meta charset='utf-8'><title>Memory Hybrid — Overview Dashboard</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:Segoe UI,Arial,sans-serif;padding:32px;background:#f6f8fa;color:#222}}
h1{{margin-bottom:4px}}
.subtitle{{color:#656d76;margin-bottom:24px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;margin-bottom:24px}}
.card{{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:20px}}
.card .num{{font-size:32px;font-weight:600;line-height:1.2}}
.card .lbl{{font-size:13px;color:#656d76;margin-top:4px}}
.card .sub{{font-size:12px;color:#656d76;margin-top:8px}}
.panel{{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:20px;margin-bottom:24px}}
.panel h2{{font-size:16px;margin-bottom:12px}}
</style></head><body>
<h1>Memory Hybrid — Overview Dashboard</h1>
<p class='subtitle'>Generated {datetime.now(timezone.utc).isoformat()}</p>
<div class='grid'>
    <div class='card'><div class='num'>{total_items}</div><div class='lbl'>Total Entries</div></div>
    <div class='card'><div class='num'>{kind_counts.get("candidate",0)}</div><div class='lbl'>Candidates</div></div>
    <div class='card'><div class='num'>{kind_counts.get("review_event",0)}</div><div class='lbl'>Review Events</div></div>
    <div class='card'><div class='num'>{kind_counts.get("rule",0)}</div><div class='lbl'>Rules</div></div>
    <div class='card'><div class='num'>{len(sorted_days)}</div><div class='lbl'>Active Days</div></div>
</div>
<div class='panel'><h2>Timeline Activity (last 30 days)</h2><div style='margin-top:12px'>{bar_chart}</div></div>
<div class='panel'><h2>Severity Breakdown</h2><div style='margin-top:12px'>{sev_bars}</div></div>
<div class='grid'>
    <div class='card'><div class='num'>{severity_counts.get("high",0)}</div><div class='lbl'>High Severity</div></div>
    <div class='card'><div class='num'>{severity_counts.get("medium",0)}</div><div class='lbl'>Medium Severity</div></div>
    <div class='card'><div class='num'>{severity_counts.get("low",0)}</div><div class='lbl'>Low Severity</div></div>
</div>
{_render_legend_html()}
</body></html>"""
    export_dir = Path(_timeline_export_dir())
    export_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sev_label = f"-severity-{severity}" if severity else ""
    path = export_dir / f"timeline-overview-{timestamp}{sev_label}.html"
    path.write_text(html, encoding="utf-8")
    return {"format": "html", "path": str(path), "file_size": path.stat().st_size, "count": total_items, "kind_counts": kind_counts, "severity_counts": severity_counts}


# ── Phase 21: Timeline Stats ──────────────────────────────────────


@app.get("/v1/layers/L0/timeline/stats")
async def timeline_stats():
    """Aggregated timeline statistics: kind/severity/level distribution, monthly trends."""
    items = unified_timeline()
    total = len(items)
    kind_dist: dict[str, int] = {}
    severity_dist: dict[str, int] = {}
    level_dist: dict[str, int] = {}
    status_dist: dict[str, int] = {}
    monthly: dict[str, int] = {}
    for item in items:
        k = str(item.get("kind", "unknown"))
        kind_dist[k] = kind_dist.get(k, 0) + 1
        pl = item.get("payload", {})
        sev = str(pl.get("severity", "none")).lower()
        severity_dist[sev] = severity_dist.get(sev, 0) + 1
        lv = str(pl.get("level", "none")).lower()
        level_dist[lv] = level_dist.get(lv, 0) + 1
        st = str(pl.get("status", "none")).lower()
        status_dist[st] = status_dist.get(st, 0) + 1
        ts = str(item.get("timestamp", ""))
        if ts and len(ts) >= 7:
            month_key = ts[:7]  # YYYY-MM
            monthly[month_key] = monthly.get(month_key, 0) + 1
    return {
        "total": total,
        "kind_distribution": kind_dist,
        "severity_distribution": severity_dist,
        "level_distribution": level_dist,
        "status_distribution": status_dist,
        "monthly_activity": dict(sorted(monthly.items())),
    }


# ── Phase 22: Auto-cleanup old exports ────────────────────────────


def _cleanup_old_exports(retention_count: int = 20) -> dict[str, Any]:
    """Remove oldest export files beyond retention_count. Skips .zip bundles."""
    from pathlib import Path
    export_dir = Path(_timeline_export_dir())
    if not export_dir.exists():
        return {"cleaned": 0}
    all_files = sorted(export_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for f in all_files:
        if f.suffix not in (".html", ".md", ".json"):
            continue
        if f.suffix == ".html" and f.name.startswith("timeline-batch-"):
            continue
    kept = 0
    for f in sorted(export_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix not in (".html", ".md", ".json"):
            continue
        if f.suffix == ".html" and f.name.startswith("timeline-batch-"):
            continue  # keep all zip bundles
        kept += 1
        if kept > retention_count:
            f.unlink()
            removed += 1
    return {"cleaned": removed, "retention": retention_count}


@app.post("/v1/layers/L0/timeline/export/cleanup")
async def cleanup_timeline_exports(retention: int = 20):
    """Remove oldest export files beyond retention count."""
    result = _cleanup_old_exports(retention_count=retention)
    return result


# ── Phase 23: Enhanced HTML Legend ─────────────────────────────────


_SEVERITY_STYLES = {
    "critical": {"bg": "#ffd8d8", "border": "#ff0000", "label": "Critical — immediate attention required"},
    "high": {"bg": "#ffebe9", "border": "#ff8182", "label": "High — should be reviewed soon"},
    "medium": {"bg": "#fff8c5", "border": "#d4a72c", "label": "Medium — worth monitoring"},
    "low": {"bg": "#dafbe1", "border": "#1a7f37", "label": "Low — informational"},
    "none": {"bg": "#f6f8fa", "border": "#d0d7de", "label": "None — no severity set"},
}


def _render_legend_html() -> str:
    cards = "".join(
        f"<div style='background:{info['bg']};border:1px solid {info['border']};border-radius:6px;padding:8px 12px;font-size:13px'><strong>{sev}</strong>: {info['label']}</div>"
        for sev, info in _SEVERITY_STYLES.items()
    )
    return (
        "<details style='margin-bottom:20px'><summary style='cursor:pointer;font-weight:600;font-size:15px'>Severity &amp; Level Legend</summary>"
        f"<div style='margin-top:8px;display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px'>{cards}</div>"
        "<hr style='margin:12px 0'>"
        "<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;font-size:13px'>"
        "<div><strong>Levels:</strong><br><code>soft</code> = guideline, <code>hard</code> = enforced</div>"
        "<div><strong>Statuses:</strong><br><code>pending</code> / <code>approved</code> / <code>rejected</code></div>"
        "<div><strong>Kinds:</strong><br><code>candidate</code> / <code>review_event</code> / <code>rule</code></div>"
        "</div></details>"
    )


@app.get("/v1/layers/L0/timeline/export")
async def export_hardening_timeline(
    kind: str | None = None,
    candidate_id: str | None = None,
    rule_id: str | None = None,
    severity: str | None = None,
    since: str | None = None,
    until: str | None = None,
    recent: int | None = None,
    export_format: str = "html",
    # Phase 20 enhanced filters
    search: str | None = None,
    status: str | None = None,
    level: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
):
    payload = await get_hardening_timeline(
        kind=kind,
        candidate_id=candidate_id,
        rule_id=rule_id,
        severity=severity,
        since=since,
        until=until,
        recent=recent,
        search=search,
        status=status,
        level=level,
        offset=offset,
        limit=limit,
    )
    payload["filters"] = {
        "kind": kind,
        "candidate_id": candidate_id,
        "rule_id": rule_id,
        "severity": severity,
        "since": since,
        "until": until,
        "recent": recent,
        "search": search,
        "status": status,
        "level": level,
        "export_format": export_format,
    }
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload["memory_root"] = str(preferred_memory_root() or ensure_memory_root())
    payload["latest_benchmark_report"] = _latest_benchmark_report_path()
    payload["latest_rules_snapshot"] = _latest_rules_snapshot_path()
    export_info = _write_timeline_export(payload, export_format)
    # Phase 22: auto-cleanup after export
    _cleanup_old_exports(retention_count=20)
    return {"format": export_format, **export_info, "count": payload["count"], "total": payload["total"]}


@app.get("/v1/layers/L0/remediation-candidates/auto-approve")
async def list_auto_approve_remediation_candidates():
    items = filter_candidate_reviews(status="pending")
    items = [item for item in items if item.get("auto_approve_recommended") is True]
    return {"count": len(items), "items": sort_candidates(items, by_priority=True)}


@app.get("/v1/memory-root")
async def get_preferred_memory_root():
    root = preferred_memory_root()
    return {"memory_root": str(root) if root else None}


@app.post("/v1/memory-root/init")
async def init_memory_root():
    root = ensure_memory_root()
    return {"memory_root": str(root)}


# ── Unified Recall (Memory Router) ───────────────────────────────


@app.get("/v1/recall")
async def unified_recall(
    query: str = Query(...),
    layers: str | None = Query(None),  # comma-separated, e.g. "L3,L4"
    top_k: int = Query(5),
    task_mode: TaskMode = Query(TaskMode.GENERAL),
    # Phase 31: temporal scoring
    temporal_mode: TemporalMode = Query(TemporalMode.AUTO),
):
    """Unified recall: auto-classifies query → queries target layers → merges results.

    Supports temporal reranking via temporal_mode parameter:
    - auto: classify temporal intent from query text
    - recent: boost latest items
    - past: boost older items
    - current: only items within 24h
    - all: no temporal bias
    """
    target_layers = layers.split(",") if layers else classify_query(query, task_mode=task_mode)
    resolved_mode = classify_temporal_intent(query) if temporal_mode == TemporalMode.AUTO else temporal_mode

    all_results: list[MemoryItem] = []

    for layer in target_layers:
        try:
            if layer == "L2" and neo4j:
                raw = neo4j.search_memory(query, top_k)
                all_results.extend(MemoryItem(**item) for item in raw)
            elif layer in ("L3", "L4", "L5") and qdrant:
                col_map = {"L3": "temporal_memory", "L4": "semantic_memory", "L5": "aspiration_memory"}
                raw = await qdrant.search(col_map[layer], query, top_k)
                all_results.extend(MemoryItem(**item) for item in raw)
        except Exception as e:
            print(f"[router] layer {layer} failed: {e}")

    for item in all_results:
        _ = score_item(item, task_mode)
        # Apply temporal scoring on top of task-weighted score
        created_value = item.metadata.get("created_at", "")
        created_str = created_value if isinstance(created_value, str) else ""
        item.score = score_temporal(created_str, mode=resolved_mode, base_score=item.score)

    deduped = dedupe_ranked(all_results)

    return deduped[:top_k]


# ── Phase 25: Export file listing & serving ───────────────────────


@app.get("/v1/layers/L0/timeline/exports")
async def list_timeline_exports(kind_filter: str | None = None):
    """List all export files with metadata."""
    from pathlib import Path
    export_dir = Path(_timeline_export_dir())
    if not export_dir.exists():
        return {"count": 0, "exports": []}
    files = []
    for f in sorted(export_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        if kind_filter and kind_filter not in f.name:
            continue
        files.append({
            "filename": f.name,
            "path": str(f),
            "file_size": f.stat().st_size,
            "file_size_hr": _human_size(f.stat().st_size),
            "modified_at": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
            "is_zip": f.suffix == ".zip",
        })
    return {"count": len(files), "exports": files}


@app.get("/v1/layers/L0/timeline/exports/content")
async def get_timeline_export_content(filename: str):
    """Read and return the content of a specific export file."""
    from pathlib import Path
    export_dir = Path(_timeline_export_dir())
    file_path = export_dir / filename
    if not file_path.exists() or not file_path.is_file():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Export file not found: {filename}")
    content = file_path.read_text(encoding="utf-8")
    return {
        "filename": filename,
        "file_size": file_path.stat().st_size,
        "content": content,
    }


# ── Phase 26: Health / Status for agent diagnostics ──────────────


@app.get("/v1/health")
async def system_health():
    """Comprehensive health check for memory-hybrid system."""
    from pathlib import Path
    status: dict[str, str] = {}
    checks: dict[str, Any] = {}

    # Memory root
    root = preferred_memory_root()
    if root and root.exists():
        memory_root_status = "ok"
        try:
            root_items = len(list(root.rglob("*")))
            checks["memory_root"] = {"path": str(root), "items": root_items}
        except Exception:
            checks["memory_root"] = {"path": str(root), "items": -1}
    else:
        memory_root_status = "missing"

    # Rules file
    from .review_flow import rules_file_path, review_log_path, candidate_review_dir
    rules_path = rules_file_path()
    if rules_path.exists():
        import yaml
        rules_data = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
        rule_count = len(rules_data.get("rules", [])) if isinstance(rules_data, dict) else 0
        checks["rules"] = {"path": str(rules_path), "count": rule_count}
    else:
        checks["rules"] = {"path": str(rules_path), "count": 0}

    # Review log
    review_path = review_log_path()
    if review_path.exists():
        import yaml
        log_data = yaml.safe_load(review_path.read_text(encoding="utf-8"))
        event_count = len(log_data.get("events", [])) if isinstance(log_data, dict) else 0
        checks["review_log"] = {"path": str(review_path), "events": event_count}

    # Candidates
    candidates_dir = candidate_review_dir()
    if candidates_dir.exists():
        candidate_count = len(list(candidates_dir.glob("*.yaml")))
        checks["candidates"] = {"path": str(candidates_dir), "count": candidate_count}

    # Backend connections
    checks["qdrant"] = {"available": qdrant is not None}
    checks["neo4j"] = {"available": neo4j is not None}

    # Timeline
    timeline = unified_timeline()
    checks["timeline"] = {"total_entries": len(timeline)}

    # Export dir
    export_dir = Path(_timeline_export_dir())
    if export_dir.exists():
        export_count = len(list(export_dir.iterdir()))
        checks["exports"] = {"path": str(export_dir), "count": export_count}

    overall = "ok" if memory_root_status == "ok" else "degraded"
    return {"status": overall, "checks": checks}


# ── Phase 27: Candidate promote-to-rule ──────────────────────────


@app.post("/v1/layers/L0/candidates/{candidate_id}/promote")
async def promote_candidate_to_rule(candidate_id: str):
    """Promote an approved candidate into a permanent hardening rule."""
    from pathlib import Path
    import yaml
    from .review_flow import rules_file_path, candidate_review_dir, _log_review_action

    # Find candidate
    candidate_dir = candidate_review_dir()
    candidate_path = candidate_dir / f"{candidate_id}.yaml"
    if not candidate_path.exists():
        raise HTTPException(status_code=404, detail=f"Candidate not found: {candidate_id}")
    candidate = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    if not candidate:
        raise HTTPException(status_code=400, detail="Empty candidate file")

    # Load existing rules
    rules_path = rules_file_path()
    if rules_path.exists():
        rules_data = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {"rules": []}
    else:
        rules_data = {"rules": []}
    rules = rules_data.get("rules", [])

    # Create rule from candidate
    rule_id = f"HARDEN-{candidate_id}"
    # Check if already exists
    if any(rule.get("id") == rule_id for rule in rules):
        return {"rule_id": rule_id, "action": "already_exists"}

    new_rule = {
        "id": rule_id,
        "pattern": candidate.get("pattern", ""),
        "corrective": candidate.get("corrective", ""),
        "trigger": candidate.get("trigger", ""),
        "level": candidate.get("level", "soft"),
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_candidate": candidate_id,
    }
    rules.append(new_rule)
    rules_data["rules"] = rules
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(yaml.safe_dump(rules_data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # Update candidate status
    candidate["status"] = "promoted"
    candidate_path.write_text(yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False), encoding="utf-8")

    _log_review_action(candidate_id, "promoted")
    return {"rule_id": rule_id, "action": "created", "rule": new_rule}


# ── Phase 28: Session memory tracking ─────────────────────────────


@app.get("/v1/sessions")
async def list_sessions(recent: int | None = None):
    """List agent session records stored in the memory system."""
    from pathlib import Path
    root = preferred_memory_root() or ensure_memory_root()
    sessions_dir = root / "sessions"
    if not sessions_dir.exists():
        return {"count": 0, "sessions": []}
    files = sorted(sessions_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if recent:
        files = files[:recent]
    sessions = []
    for f in files:
        content = f.read_text(encoding="utf-8", errors="replace")[:500]
        sessions.append({
            "filename": f.name,
            "path": str(f),
            "file_size": f.stat().st_size,
            "modified_at": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
            "preview": content[:200],
        })
    return {"count": len(sessions), "sessions": sessions}


@app.post("/v1/sessions/record")
async def record_session(agent_name: str = Query(...), status: str = Query("active"), summary: str | None = Query(None)):
    """Record an agent session into the memory system."""
    from pathlib import Path
    root = preferred_memory_root() or ensure_memory_root()
    sessions_dir = root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc)
    filename = f"SESSION-{ts.strftime('%Y%m%dT%H%M%SZ')}-{agent_name}.md"
    content = f"""# Session: {agent_name}
- timestamp: {ts.isoformat()}
- agent: {agent_name}
- status: {status}
"""
    if summary:
        content += f"- summary: {summary}\n"
    (sessions_dir / filename).write_text(content, encoding="utf-8")
    return {"filename": filename, "agent": agent_name, "status": status, "timestamp": ts.isoformat()}


# ── Phase 29: Export with Accept header negotiation ──────────────


@app.get("/v1/layers/L0/timeline/export/auto")
async def export_timeline_auto(
    request: Request,
    kind: str | None = None,
    severity: str | None = None,
    search: str | None = None,
    status: str | None = None,
    level: str | None = None,
):
    """Auto-detect export format from Accept header (Phase 29)."""
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        fmt = "json"
    elif "text/markdown" in accept or "text/x-markdown" in accept:
        fmt = "markdown"
    else:
        fmt = "html"
    return await export_hardening_timeline(
        kind=kind, severity=severity,
        search=search, status=status, level=level,
        export_format=fmt,
    )


# ── Phase 30: Memory compact / archive ────────────────────────────


@app.post("/v1/memory/compact")
async def compact_memory(dry_run: bool = True):
    """Archive decayed items and clean up stale state files.

    In dry_run mode, reports what would be archived without making changes.
    """
    from pathlib import Path
    root = preferred_memory_root() or ensure_memory_root()
    report: dict[str, Any] = {"dry_run": dry_run, "archived": [], "cleaned": []}

    # Archive old session files (older than 30 days)
    sessions_dir = root / "sessions"
    archive_dir = root / "sessions" / "sessions-archive"
    threshold_ts = datetime.now(timezone.utc).timestamp() - (30 * 86400)
    if sessions_dir.exists():
        for f in sessions_dir.glob("SESSION-*.md"):
            if f.stat().st_mtime < threshold_ts:
                report["archived"].append(f.name)
                if not dry_run:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    f.rename(archive_dir / f.name)

    # Remove empty timeline directories
    timeline_dir = root / "timeline"
    if timeline_dir.exists():
        for year_dir in timeline_dir.iterdir():
            if year_dir.is_dir():
                for month_dir in year_dir.iterdir():
                    if month_dir.is_dir() and not list(month_dir.iterdir()):
                        report["cleaned"].append(str(month_dir))
                        if not dry_run:
                            month_dir.rmdir()

    report["archived_count"] = len(report["archived"])
    report["cleaned_count"] = len(report["cleaned"])
    return report


def _render_timeline_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Memory Hybrid Timeline Export", ""]
    lines.append(f"- count: {payload['count']}")
    lines.append(f"- stats: {payload['stats']}")
    filters = payload.get("filters", {})
    if filters:
        lines.append(f"- filters: {filters}")
    lines.append("")
    for item in payload["items"]:
        lines.append(f"## {item.get('kind')} :: {item.get('id')}")
        lines.append(f"- timestamp: {item.get('timestamp', '')}")
        lines.append(f"- payload: {item.get('payload')}")
        lines.append("")
    return "\n".join(lines)


def _render_timeline_html(payload: dict[str, Any]) -> str:
    filters = payload.get("filters", {})
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in payload["items"]:
        groups.setdefault(str(item.get("kind", "unknown")), []).append(item)
    sections = []
    for kind, items in groups.items():
        rows = []
        for item in items:
            payload_obj = item.get("payload", {})
            severity = str(payload_obj.get("severity", "")).lower()
            sev_style = ""
            if severity == "high":
                sev_style = "background:#ffebe9;"
            elif severity == "medium":
                sev_style = "background:#fff8c5;"
            # Related rule: link if file path, else plain text
            related_rule_raw = payload_obj.get("rule_id") or (f"HARDEN-{item.get('id')}" if kind == "candidate" else "")
            related_rule = related_rule_raw
            if related_rule_raw and (related_rule_raw.startswith("/") or related_rule_raw.startswith(".") or "\\" in related_rule_raw):
                related_rule_html = f"<a href='file:///{related_rule_raw}'>{related_rule_raw}</a>"
            else:
                related_rule_html = related_rule_raw
            # Benchmark link: clickable if file path
            benchmark_raw = payload_obj.get("benchmark_report") or ""
            if benchmark_raw and (benchmark_raw.startswith("/") or benchmark_raw.startswith(".") or "\\" in benchmark_raw):
                benchmark_html = f"<a href='file:///{benchmark_raw}'>{benchmark_raw.split('/')[-1].split('\\\\')[-1]}</a>"
            else:
                benchmark_html = benchmark_raw
            rows.append(
                f"<tr id='{item.get('id','')}' style='{sev_style}'><td>{item.get('id','')}</td><td>{item.get('timestamp','')}</td><td>{related_rule_html}</td><td>{benchmark_html}</td><td><pre>{payload_obj}</pre></td></tr>"
            )
        sections.append(
            f"<h2 id='{kind}'>{kind}</h2><table><thead><tr><th>ID</th><th>Timestamp</th><th>Related Rule</th><th>Benchmark</th><th>Payload</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
        )
    return (
        "<html><head><meta charset='utf-8'><title>Memory Hybrid Timeline</title>"
        "<style>body{font-family:Segoe UI,Arial,sans-serif;padding:24px;color:#222}.meta{background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:16px;margin-bottom:20px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #d0d7de;padding:8px;vertical-align:top}th{background:#f6f8fa;text-align:left}pre{white-space:pre-wrap;margin:0}.summary{display:grid;grid-template-columns:repeat(2,minmax(200px,1fr));gap:12px;margin:12px 0}.card{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:12px}.file-link{font-family:monospace;font-size:0.9em;word-break:break-all}</style></head><body>"
        f"<h1>Memory Hybrid Timeline Export</h1>"
        f"<div class='meta'><div><strong>count</strong>: {payload['count']}</div>"
        f"<div><strong>generated_at</strong>: {payload.get('generated_at','')}</div>"
        f"<div><strong>memory_root</strong>: {payload.get('memory_root','')}</div>"
        f"<div><strong>filters</strong>: {filters}</div>"
        f"<div class='summary'><div class='card'><strong>stats</strong><pre>{payload['stats']}</pre></div><div class='card'><strong>count</strong><pre>{payload['count']}</pre></div><div class='card'><strong>filters</strong><pre>{filters}</pre></div></div></div>"
        f"<div class='meta'><strong>latest_benchmark</strong>: {payload.get('latest_benchmark_report','')}<br/><strong>latest_rules_snapshot</strong>: {payload.get('latest_rules_snapshot','')}</div>"
        + _render_legend_html()
        + f"<h2>Contents</h2><ul><li><a href='#candidate'>candidate ({len(groups.get('candidate',[]))})</a></li><li><a href='#review_event'>review_event ({len(groups.get('review_event',[]))})</a></li><li><a href='#rule'>rule ({len(groups.get('rule',[]))})</a></li></ul>"
        + "".join(sections)
        + "</body></html>"
    )


def _timeline_export_dir() -> str:
    return str((preferred_memory_root() or ensure_memory_root()) / "exports" / "timeline")


def _latest_benchmark_report_path() -> str:
    from pathlib import Path

    reports_dir = Path(__file__).resolve().parents[1] / "tools" / "reports"
    reports = sorted(reports_dir.glob("benchmark-*.json"))
    return str(reports[-1]) if reports else ""


def _latest_rules_snapshot_path() -> str:
    history_dir = (preferred_memory_root() or ensure_memory_root()) / "hardening" / "history"
    snapshots = sorted(history_dir.glob("*.yaml"))
    return str(snapshots[-1]) if snapshots else ""


def _write_timeline_export(payload: dict[str, Any], export_format: str) -> dict[str, Any]:
    from pathlib import Path

    export_dir = Path(_timeline_export_dir())
    export_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label_parts = []
    for key in ("kind", "candidate_id", "rule_id", "severity"):
        value = payload.get("filters", {}).get(key)
        if value:
            label_parts.append(f"{key}-{value}")
    label = ("-" + "-".join(label_parts)) if label_parts else ""
    ext = {"html": ".html", "markdown": ".md", "json": ".json"}[export_format]
    path = export_dir / f"timeline-{timestamp}{label}{ext}"
    if export_format == "html":
        path.write_text(_render_timeline_html(payload), encoding="utf-8")
    elif export_format == "markdown":
        path.write_text(_render_timeline_markdown(payload), encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    file_size = path.stat().st_size
    return {
        "path": str(path),
        "filename": path.name,
        "format": export_format,
        "file_size": file_size,
        "file_size_hr": _human_size(file_size),
        "written_at": timestamp,
    }


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


# ── Dashboard (Phase 32) ──────────────────────────────────────────

from .dashboard import router as dashboard_router
app.include_router(dashboard_router)
