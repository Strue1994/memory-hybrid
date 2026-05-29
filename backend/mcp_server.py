"""MCP Server for memory-hybrid system.

Provides Model Context Protocol tools for agent integration.
Uses SQLite (sqlite-vec + FTS5 + graph CTE) as primary store.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from storage.sqlite_store import get_store
from storage.file_mirror import FileMirror

# ── Configuration ────────────────────────────────────────────────

BACKEND_URL = os.environ.get("BACKEND_URL", "")  # optional HTTP backend
DB_PATH = os.environ.get("MEMORY_DB_PATH", "")   # optional custom DB path

# Lazy-init store & mirror
_store = None
_mirror = FileMirror()


def _store_singleton():
    global _store
    if _store is None:
        _store = get_store(DB_PATH if DB_PATH else None)
    return _store


# ── MCP Server ───────────────────────────────────────────────────

mcp = FastMCP(
    "memory-hybrid",
    instructions="Offline-first memory system with SQLite (vector + FTS5 + graph). "
                "L3=temporal, L4=facts, L5=goals, L6=decisions, L2=relationships, L0=rules.",
)


# ── Helpers ──────────────────────────────────────────────────────


def _json(val: Any, **kw: Any) -> str:
    return json.dumps(val, ensure_ascii=False, default=str, **kw)


# ── Tools ────────────────────────────────────────────────────────


@mcp.tool()
def recall(query: str, layers: str = "L3,L4", top_k: int = 5) -> str:
    """Recall memory entries matching a query across specified layers.

    Uses hybrid search (FTS5 BM25 + sqlite-vec cosine similarity, RRF merged)
    for optimal recall precision.

    Args:
        query: Search text to match against memory content.
        layers: Comma-separated layer list (L3=temporal, L4=facts, L5=goals, L6=decisions).
        top_k: Maximum results to return.
    """
    if BACKEND_URL:
        import httpx
        try:
            resp = httpx.get(f"{BACKEND_URL}/v1/recall", params={"query": query, "layers": layers, "top_k": top_k}, timeout=10)
            resp.raise_for_status()
            return _json(resp.json(), indent=2)
        except Exception as e:
            return _json({"error": f"Backend unavailable: {e}"})

    target_layers = [l.strip().upper() for l in layers.split(",")]
    store = _store_singleton()
    results = store.recall(query, layers=target_layers, top_k=top_k, mode="hybrid")

    if not results:
        return _json({"query": query, "layers": target_layers, "count": 0, "results": []})
    return _json({
        "query": query,
        "layers": target_layers,
        "count": len(results),
        "results": results,
    }, indent=2)


@mcp.tool()
def health() -> str:
    """Return system health status as JSON."""
    store = _store_singleton()
    h = store.health()
    stats = store.memory_stats()
    return _json({
        "status": h["status"],
        "db_path": h["db_path"],
        "db_size": h["db_size"],
        "embedding_available": h["embedding_available"],
        "stats": stats,
    }, indent=2)


@mcp.tool()
def list_sessions(recent: int = 5) -> str:
    """List recent agent session records.

    Args:
        recent: Number of most recent sessions to return.
    """
    store = _store_singleton()
    sessions = store.list_sessions(recent)
    return _json({"count": len(sessions), "sessions": sessions}, indent=2)


@mcp.tool()
def record_session(agent_name: str, status: str = "active", summary: str = "") -> str:
    """Record a new agent session into the memory system.

    Args:
        agent_name: Name of the agent.
        status: Session status (active, completed, failed).
        summary: Optional summary of session activity.
    """
    store = _store_singleton()
    session_id = store.record_session(agent_name, status, summary)
    _mirror.on_session_recorded(agent_name, summary, session_id)
    ts = datetime.now(timezone.utc)
    return _json({
        "session_id": session_id,
        "agent": agent_name,
        "status": status,
        "timestamp": ts.isoformat(),
    })


@mcp.tool()
def list_rules() -> str:
    """List hardening rules from the memory system."""
    store = _store_singleton()
    rules = store.list_rules()
    return _json({"count": len(rules), "rules": rules}, indent=2, ensure_ascii=False)


@mcp.tool()
def memory_stats() -> str:
    """Return aggregate statistics about the memory system."""
    store = _store_singleton()
    stats = store.memory_stats()
    return _json(stats, indent=2)


@mcp.tool()
def save_memory(layer: str, content: str, metadata_json: str = "{}") -> str:
    """Save a memory entry into the SQLite store.

    Args:
        layer: Layer identifier — L3 (temporal), L4 (facts), L5 (goals), L6 (decisions).
        content: The memory content text.
        metadata_json: Optional JSON string with metadata fields.

    Returns:
        JSON with id and layer of the saved memory.
    """
    metadata = json.loads(metadata_json) if isinstance(metadata_json, str) else metadata_json
    store = _store_singleton()
    mem_id = store.save_memory(layer, content, metadata)
    _mirror.on_memory_saved(layer, content, metadata, mem_id)
    return _json({"id": mem_id, "layer": layer})


@mcp.tool()
def graph_search(query: str, top_k: int = 5) -> str:
    """Search the knowledge graph (L2) for people, skills, and relationships.

    Uses recursive CTEs for relationship traversal across the graph.
    Results include direct connections and relationship types.

    Args:
        query: Person name, skill name, or keyword to search for.
        top_k: Maximum results to return.

    Returns:
        JSON with matched graph nodes and their relationships.
    """
    store = _store_singleton()
    results = store.query_graph(query, top_k)
    return _json({"query": query, "count": len(results), "results": results}, indent=2)


@mcp.tool()
def save_rule(
    rule_id: str,
    trigger: str,
    pattern: str,
    corrective: str,
    level: str = "soft",
    source_decision_id: str = "",
) -> str:
    """Save a hardening rule (L0) into the memory system.

    Args:
        rule_id: Unique identifier for the rule.
        trigger: Condition that triggers this rule.
        pattern: The behavioral pattern to detect.
        corrective: The corrective action to apply.
        level: Rule level — soft (guideline) or hard (enforced).
        source_decision_id: Optional reference to the originating L6 decision.

    Returns:
        JSON with the saved rule details.
    """
    store = _store_singleton()
    rule = store.save_rule(rule_id, trigger, pattern, corrective, level, source_decision_id)
    _mirror.on_rules_changed(store.list_rules())
    return _json(rule, indent=2)


# ── On-Demand Layer Guides ───────────────────────────────────────

_LAYER_GUIDES: dict[str, str] = {
    "L0": """## L0 — 行为固化 (Behavior Hardening)

**类型**: 纯文件 \u00b7 无后端依赖
**目的**: 防止 Agent 行为漂移

**目录结构**:
  memory-hybrid/hardening/
    rules.yaml        # 固化规则定义
    strikes.log       # 2-Strike 偏差记录
    selectors/        # Bselect 行为选择器
    candidates/       # 待审批硬化候选
    review-log.yaml   # 审批日志
    history/          # rules.yaml 历史快照

**3 级固化**: soft(可override) / hard(必须执行) / invariant(不可绕过)

**硬化流程**: 重复模式 -> L6 决策 -> hardening candidate -> rules.yaml
**规则格式**: id / trigger / pattern / level / anti_pattern / corrective
""",
    "L1": """## L1 — 会话连续 (Session Continuity)

**类型**: 纯文件 \u00b7 无后端依赖
**目的**: 跨会话保持上下文

**文件位置**:
  memory-hybrid/sessions/
    SESSION-STATE.md       # 当前会话状态
    sessions-archive/      # 历史会话存档

**SESSION-STATE.md 模板**:
  - session_id / start / last_action
  - decisions / pending items / context_hash

**跨会话恢复**: 加载 SESSION-STATE.md -> 重建 decisions -> 重建 pending -> 校验 context_hash
""",
    "L2": """## L2 — 人物画像 + 关系图谱 (Persona & Relationship Graph)

**类型**: 混合 (文件 + 可选 Neo4j)
**目的**: 维护 Agent 自画像、交互人物关系、技能关联

**文件层 (无后端)**:
  memory-hybrid/profiles/
    self.md           # Agent 自画像
    humans/           # 人类交互者画像
    agents/           # 其他 Agent 画像

**Neo4j 层 (有后端时)**: KNOWS / HAS_SKILL / MENTIONS / COLLABORATED_ON 关系

**同步机制**: 文件 <-> Neo4j 双向同步, Neo4j 不可用时纯文件模式
""",
    "L3": """## L3 — 时间记忆 (Temporal Memory)

**类型**: 混合 (文件 + 可选 Qdrant)
**目的**: 按时间线记录 Agent 工作日志

**文件结构**:
  memory-hybrid/timeline/YYYY/MM/YYYY-MM-DD.md

**每日日志模板**:
  # YYYY-MM-DD
  - project / focus / sessions
  - decisions / facts_learned / blockers

**检索**: recall(query, layers="L3") 或 GET /v1/layers/L3/recall

**升降级**: 后端可用 -> 写入文件+Qdrant; 不可用 -> 纯文件; 恢复 -> 批量同步
""",
    "L4": """## L4 — 事实策展 (Fact Curation)

**类型**: 混合 (文件 + 可选 Qdrant)
**目的**: 从 L3 日志萃取可复用事实, 2-Strike 验证后固化

**策展管道**: Observe -> Validate (2-Strike) -> Curate -> Harden

**文件结构**:
  memory-hybrid/facts/
    curated/          # 已策展事实 (<domain>.md)
    pending/          # 待验证事实

**事实格式**: fact / importance / source / verified / strike_count

**验证规则**: strike_count>=2 -> verified, strike_count>=3 -> importance+1
""",
    "L5": """## L5 — 数字自我 (Digital Self / Goals)

**类型**: 混合 (文件 + 可选 Qdrant + API)
**目的**: 管理 Agent 长期目标、能力边界、外部身份

**文件结构**:
  memory-hybrid/goals/
    active.yaml       # 活跃目标
    completed.yaml    # 已完成目标
    archived/         # 归档目标

**目标格式**: id / title / priority / status / progress / deadline / related_skills

**外部 API 桥接**: EXTERNAL_PROFILE_URL / EXTERNAL_GOAL_SYNC_URL / EXTERNAL_REFLECTION_URL
""",
    "L6": """## L6 — 决策审计 (Decision Audit)

**类型**: 纯文件 \u00b7 无后端依赖
**目的**: 记录关键决策路径, 支持回溯

**文件结构**:
  memory-hybrid/decisions/YYYY/MM/YYYY-MM-DD-<seq>.md
  memory-hybrid/state-machines/<task-name>.yaml

**决策模板**:
  # Decision <type>: <title>
  - decision_id / timestamp / trigger
  - options (selected/rejected)
  - rationale / outcome / confidence

**晋升路径**: L6 决策 -> hardening candidate -> rules.yaml
""",
    "router": """## Memory Router — 查询路由

**类型**: 启发式分类器 / API 路由
**目的**: 确定查询哪些层以及合并结果

**路由规则**:
  - 含日期/时间词 -> L3 (时间记忆)
  - 含人物名 -> L2 (关系) + L3 + L4 (事实)
  - 含技能/知识词 -> L4 + L3
  - 含目标/计划词 -> L5 (目标)
  - 含"为什么"/决策词 -> L6 (决策审计)
  - 否则 -> 全层检索

**Task-aware 路由**:
  general: 均衡 | debug: L6>L3>L4 | implement: L4>L3>L5
  plan: L5>L6>L4 | social: L2>L3>L6

**合并策略**: Score = layer_weight * score * recency_bonus
  layer_weight: L2=0.8, L3=1.0, L4=1.2, L5=0.6, L6=0.7
  recency_bonus: 24h内+0.3, 7d内+0.1
""",
}

_PRESETS = {
    "minimal": {
        "description": "临时/一次性任务, 不需要后端",
        "layers": ["L0", "L1"],
    },
    "knowledge-worker": {
        "description": "编码、文档、研究类任务, 推荐 Qdrant",
        "layers": ["L0", "L1", "L3", "L4", "L6"],
    },
    "social-agent": {
        "description": "多人协作、项目管理, 推荐 Qdrant+Neo4j",
        "layers": ["L0", "L1", "L2", "L3", "L5", "L6"],
    },
    "full": {
        "description": "生产级全能力, 需要 FastAPI+Qdrant+Neo4j",
        "layers": ["L0", "L1", "L2", "L3", "L4", "L5", "L6", "router"],
    },
}


@mcp.tool()
def get_layer_guide(layer: str) -> str:
    """Get detailed usage guide for a specific memory layer.

    Args:
        layer: Layer identifier — L0, L1, L2, L3, L4, L5, L6, or "router".

    Returns:
        Layer guide markdown text. Use this ON DEMAND — do NOT read all layers at once.
    """
    guide = _LAYER_GUIDES.get(layer.upper())
    if guide is None:
        keys = ", ".join(_LAYER_GUIDES.keys())
        return f"Unknown layer '{layer}'. Available: {keys}"
    return guide


@mcp.tool()
def get_preset(preset: str) -> str:
    """Get layer configuration for a preset profile.

    Args:
        preset: One of: minimal, knowledge-worker, social-agent, full.

    Returns:
        JSON with layers list and description. Use this to decide which layers to activate.
    """
    p = _PRESETS.get(preset.lower())
    if p is None:
        keys = ", ".join(_PRESETS.keys())
        return f"Unknown preset '{preset}'. Available: {keys}"
    return _json(p)


# ── Entrypoint ───────────────────────────────────────────────────


if __name__ == "__main__":
    mcp.run()
