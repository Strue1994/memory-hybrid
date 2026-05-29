"""SQLite schema definitions and version management for memory-hybrid."""

SCHEMA_VERSION = 1

# ── Core tables ───────────────────────────────────────────────────

CREATE_MEMORY_ITEMS = """
CREATE TABLE IF NOT EXISTS memory_items (
    id          TEXT PRIMARY KEY,
    layer       TEXT NOT NULL,       -- L3, L4, L5, L6
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',  -- JSON blob
    lifecycle_state TEXT NOT NULL DEFAULT 'captured',
    decay_score REAL NOT NULL DEFAULT 0.0,
    archived    INTEGER NOT NULL DEFAULT 0
);
"""

CREATE_MEMORY_ITEMS_IDX = """
CREATE INDEX IF NOT EXISTS idx_memory_layer ON memory_items(layer);
CREATE INDEX IF NOT EXISTS idx_memory_archived ON memory_items(archived);
CREATE INDEX IF NOT EXISTS idx_memory_created ON memory_items(created_at);
CREATE INDEX IF NOT EXISTS idx_memory_lifecycle ON memory_items(lifecycle_state);
"""

# FTS5 full-text search index (syncs with memory_items automatically)
CREATE_MEMORY_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    layer UNINDEXED,
    metadata,
    tokenize='unicode61',
    content=memory_items,
    content_rowid=rowid
);
"""

# Triggers to keep FTS in sync with memory_items
MEMORY_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memory_items BEGIN
    INSERT INTO memory_fts(rowid, content, layer, metadata)
    VALUES (new.rowid, new.content, new.layer, new.metadata);
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON memory_items BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, layer, metadata)
    VALUES ('delete', old.rowid, old.content, old.layer, old.metadata);
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE ON memory_items BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, layer, metadata)
    VALUES ('delete', old.rowid, old.content, old.layer, old.metadata);
    INSERT INTO memory_fts(rowid, content, layer, metadata)
    VALUES (new.rowid, new.content, new.layer, new.metadata);
END;
"""

# sqlite-vec virtual table for vector search
# Dimension is configurable; 384 is default for all-MiniLM-L6-v2
CREATE_MEMORY_VEC = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
    embedding float[384]
);
"""

# ── Knowledge graph (L2) ──────────────────────────────────────────

CREATE_GRAPH_NODES = """
CREATE TABLE IF NOT EXISTS graph_nodes (
    id       TEXT PRIMARY KEY,
    type     TEXT NOT NULL,   -- person, skill, project, tool
    name     TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(type);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_name ON graph_nodes(name);
"""

CREATE_GRAPH_EDGES = """
CREATE TABLE IF NOT EXISTS graph_edges (
    source_id    TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    relationship TEXT NOT NULL,  -- KNOWS, HAS_SKILL, MENTIONS, COLLABORATED_ON
    metadata     TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, relationship),
    FOREIGN KEY (source_id) REFERENCES graph_nodes(id),
    FOREIGN KEY (target_id) REFERENCES graph_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_rel ON graph_edges(relationship);
"""

# ── Sessions (L1) ─────────────────────────────────────────────────

CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    summary    TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_name);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
"""

# ── Hardening rules (L0) ──────────────────────────────────────────

CREATE_HARDENING_RULES = """
CREATE TABLE IF NOT EXISTS hardening_rules (
    id               TEXT PRIMARY KEY,
    trigger          TEXT NOT NULL,
    pattern          TEXT NOT NULL,
    corrective       TEXT NOT NULL,
    level            TEXT NOT NULL DEFAULT 'soft',
    enabled          INTEGER NOT NULL DEFAULT 1,
    source_decision_id TEXT DEFAULT '',
    evidence_count   INTEGER NOT NULL DEFAULT 1,
    disabled_reason  TEXT DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_enabled ON hardening_rules(enabled);
"""

# ── Schema version tracking ───────────────────────────────────────

CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

# ── All DDL statements, ordered ───────────────────────────────────

ALL_DDL = [
    CREATE_MEMORY_ITEMS,
    CREATE_MEMORY_ITEMS_IDX,
    CREATE_MEMORY_FTS,
    MEMORY_FTS_TRIGGERS,
    CREATE_MEMORY_VEC,
    CREATE_GRAPH_NODES,
    CREATE_GRAPH_EDGES,
    CREATE_SESSIONS,
    CREATE_HARDENING_RULES,
    CREATE_SCHEMA_VERSION,
]
