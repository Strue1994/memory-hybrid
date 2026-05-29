"""Unified SQLite-backed store with vector + FTS5 + graph search.

Single-file storage that replaces the file-scattered architecture:
  memory.db ─── memory_items + memory_fts + memory_vec  → L3/L4/L5/L6
              ├── graph_nodes + graph_edges              → L2
              ├── sessions                               → L1
              └── hardening_rules                        → L0

All operations are ACID via SQLite WAL mode.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from .schema import ALL_DDL, SCHEMA_VERSION

logger = logging.getLogger("memory-hybrid.store")

# ── Embedding backend abstraction ─────────────────────────────────


class EmbeddingBackend:
    """Pluggable embedding generator.

    Order of precedence:
    1. sentence-transformers (local model, offline)
    2. HTTP API (Ollama / OpenAI-compatible)
    3. None → FTS5-only mode (no vector search)
    """

    def __init__(self) -> None:
        self._model = None
        self._http_url: str = os.environ.get("EMBEDDING_URL", "")
        self._http_model: str = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        self._dimension: int = 384  # all-MiniLM-L6-v2 default
        self._lock = threading.Lock()

    def _load_local(self) -> bool:
        """Lazy-load sentence-transformers model."""
        if self._model is not None:
            return True
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._http_model)
            try:
                dim = self._model.get_embedding_dimension()
            except AttributeError:
                dim = self._model.get_sentence_embedding_dimension()
            if dim:
                self._dimension = dim
            logger.info("Loaded local embedding model: %s (dim=%d)", self._http_model, self._dimension)
            return True
        except Exception as exc:
            logger.warning("Local embedding model unavailable: %s", exc)
            return False

    def _call_http(self, texts: list[str]) -> list[list[float]] | None:
        """Call an HTTP embedding API (Ollama / OpenAI-compatible)."""
        if not self._http_url:
            return None
        try:
            import httpx

            resp = httpx.post(
                f"{self._http_url}/api/embed",
                json={"model": self._http_model, "input": texts},
                timeout=30.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                embeddings = data.get("embeddings") or data.get("data", [])
                if isinstance(embeddings, list) and len(embeddings) > 0:
                    if isinstance(embeddings[0], dict):
                        # OpenAI format: {"data": [{"embedding": [...]}]}
                        embeddings = [e["embedding"] for e in embeddings]
                    if embeddings and len(embeddings) == len(texts):
                        dim = len(embeddings[0])
                        if dim:
                            self._dimension = dim
                        return embeddings  # type: ignore[return-value]
            return None
        except Exception as exc:
            logger.debug("HTTP embedding unavailable: %s", exc)
            return None

    def embed(self, text: str) -> list[float] | None:
        """Embed a single string. Returns None if no backend available."""
        result = self.embed_batch([text])
        if result:
            return result[0]
        return None

    def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """Embed a batch of strings.

        Tries HTTP first (fast if server available), then local model.
        """
        http_result = self._call_http(texts)
        if http_result is not None:
            return http_result
        with self._lock:
            if self._load_local():
                assert self._model is not None
                vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
                return [v.tolist() for v in vecs]
        return None

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def available(self) -> bool:
        if self._http_url:
            return True
        if self._model is not None:
            return True
        # Quick probe without loading full model
        try:
            import sentence_transformers  # noqa: F401
            return True
        except ImportError:
            return False


# ── Global singleton ──────────────────────────────────────────────

_EMBEDDER = EmbeddingBackend()


def _default_db_path() -> str:
    env = os.environ.get("MEMORY_HYBRID_ROOT") or os.environ.get("MEMORY_ROOT")
    if env:
        return str(Path(env).resolve() / "memory.db")
    return str(Path(__file__).resolve().parents[2] / "memory.db")


def _serialize_f32(vec: list[float]) -> bytes:
    """Serialize a list of floats to a bytes blob (struct float32 array)."""
    import struct
    return struct.pack(f"{len(vec)}f", *vec)


def _deserialize_f32(blob: bytes) -> list[float]:
    """Deserialize a bytes blob back into a list of floats."""
    import struct
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


# ── RRF (Reciprocal Rank Fusion) ──────────────────────────────────


def _rrf_merge(
    fts_results: list[dict[str, Any]],
    vec_results: list[dict[str, Any]],
    k: int = 60,
    fts_weight: float = 1.0,
    vec_weight: float = 1.0,
) -> list[dict[str, Any]]:
    """Merge two ranked result lists using Reciprocal Rank Fusion.

    Score = fts_weight/(k + rank_fts) + vec_weight/(k + rank_vec)
    Items only in one list get full weight from that list.
    """
    id_order: dict[str, int] = {}
    id_score: dict[str, float] = {}

    for idx, item in enumerate(fts_results):
        rid = item["id"]
        id_order.setdefault(rid, idx)
        id_score[rid] = id_score.get(rid, 0.0) + fts_weight / (k + idx + 1)

    for idx, item in enumerate(vec_results):
        rid = item["id"]
        id_order.setdefault(rid, idx + len(fts_results))
        id_score[rid] = id_score.get(rid, 0.0) + vec_weight / (k + idx + 1)

    merged_by_id: dict[str, dict[str, Any]] = {}
    for item in fts_results:
        merged_by_id[item["id"]] = dict(item)
    for item in vec_results:
        if item["id"] in merged_by_id:
            existing = merged_by_id[item["id"]]
            existing["score"] = id_score[item["id"]]
            existing["fts_score"] = existing.pop("_fts_score", None)
            existing["vec_score"] = item.get("_vec_score", None)
        else:
            merged_by_id[item["id"]] = dict(item)
            merged_by_id[item["id"]]["score"] = id_score[item["id"]]

    for item in merged_by_id.values():
        item.pop("_fts_score", None)
        item.pop("_vec_score", None)

    result = sorted(merged_by_id.values(), key=lambda x: x["score"], reverse=True)
    return result


# ── Main Store ────────────────────────────────────────────────────


class SQLiteStore:
    """Unified memory store backed by a single SQLite database."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self._local = threading.local()
        self._init_db()

    # ── Connection management ──────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local connection. Each thread gets its own."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._load_vec(conn)
            self._local.conn = conn
        return conn

    @staticmethod
    def _load_vec(conn: sqlite3.Connection) -> None:
        """Load the sqlite-vec extension."""
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception as exc:
            logger.warning("sqlite-vec not available: %s — vector search disabled", exc)

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ── Schema initialization ──────────────────────────────────────

    def _init_db(self) -> None:
        """Create tables and run migrations if needed."""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        conn = self._get_conn()
        # Run all DDL (each item may contain multiple statements, so use executescript)
        for ddl in ALL_DDL:
            conn.executescript(ddl)
        conn.commit()

        # Check / update schema version
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current_version = row[0] if row and row[0] else 0

        if current_version < SCHEMA_VERSION:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            logger.info("Schema migrated to v%s", SCHEMA_VERSION)

    # ── Memory items (L3/L4/L5/L6) ─────────────────────────────────

    def save_memory(
        self,
        layer: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Save a memory item. Generates embedding automatically."""
        mem_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO memory_items (id, layer, content, created_at, updated_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (mem_id, layer, content, now, now, meta_json),
        )

        # Vector embedding
        embedding = _EMBEDDER.embed(content)
        if embedding is not None:
            vec = _serialize_f32(embedding)
            try:
                conn.execute(
                    "INSERT INTO memory_vec(rowid, embedding) VALUES (?, ?)",
                    (cursor.lastrowid, vec),
                )
            except Exception as exc:
                logger.debug("Vector insert skipped: %s", exc)

        conn.commit()
        return mem_id

    def recall(
        self,
        query: str,
        layers: list[str] | None = None,
        top_k: int = 5,
        mode: str = "hybrid",
    ) -> list[dict[str, Any]]:
        """Search memory using hybrid (FTS5 + vector) or pure FTS5.

        Args:
            query: The search text.
            layers: Layer filter (e.g. ["L3", "L4"]). None = all layers.
            top_k: Maximum results.
            mode: "hybrid" (FTS5 + vector, RRF merged), "fts" (FTS5 only),
                  "vector" (vector only).

        Returns:
            List of dicts with id, layer, content, score, metadata, created_at.
        """
        if mode == "vector":
            return self._recall_vector(query, layers, top_k)
        if mode == "fts":
            return self._recall_fts(query, layers, top_k)

        # hybrid: merge FTS5 + vector results via RRF
        fts_results = self._recall_fts(query, layers, top_k * 2)
        vec_results = self._recall_vector(query, layers, top_k * 2)

        merged = _rrf_merge(fts_results, vec_results, k=60)
        return merged[:top_k]

    def _recall_fts(
        self, query: str, layers: list[str] | None, top_k: int
    ) -> list[dict[str, Any]]:
        """Full-text search via FTS5 BM25 ranking."""
        conn = self._get_conn()
        # FTS5 query: escape special chars and use prefix matching
        q_clean = " OR ".join(
            f'"{word}"*' if word else ""
            for word in query.strip().split()
            if word
        )
        if not q_clean:
            return []

        params: list[Any] = [q_clean]
        layer_clause = ""
        if layers:
            placeholders = ",".join("?" for _ in layers)
            layer_clause = f" AND m.layer IN ({placeholders})"
            params.extend(layers)
        params.append(top_k)

        sql = f"""SELECT m.id, m.layer, m.content, m.created_at, m.metadata,
                         bm25(memory_fts, 0, 1.0) AS score
                  FROM memory_fts
                  JOIN memory_items m ON m.rowid = memory_fts.rowid
                  WHERE memory_fts MATCH ?
                    AND m.archived = 0{layer_clause}
                  ORDER BY score
                  LIMIT ?"""

        rows = conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else {}
            results.append({
                "id": row["id"],
                "layer": row["layer"],
                "content": row["content"],
                "score": float(row["score"]),
                "metadata": meta,
                "created_at": row["created_at"],
            })
        return results

    def _recall_vector(
        self, query: str, layers: list[str] | None, top_k: int
    ) -> list[dict[str, Any]]:
        """Vector similarity search via sqlite-vec KNN."""
        embedding = _EMBEDDER.embed(query)
        if embedding is None:
            return self._recall_fts(query, layers, top_k)

        conn = self._get_conn()
        vec = _serialize_f32(embedding)

        try:
            # sqlite-vec MATCH returns rowid + distance
            sql = """SELECT v.rowid, v.distance
                     FROM memory_vec v
                     WHERE v.embedding MATCH ?
                     ORDER BY v.distance
                     LIMIT ?"""
            vec_rows = conn.execute(sql, (vec, top_k * 2)).fetchall()
        except Exception as exc:
            logger.debug("Vector search failed, falling back to FTS: %s", exc)
            return self._recall_fts(query, layers, top_k)

        if not vec_rows:
            return self._recall_fts(query, layers, top_k)

        row_ids = [r[0] for r in vec_rows]
        distances = {r[0]: float(r[1]) for r in vec_rows}

        # Fetch the actual items
        id_placeholders = ",".join("?" for _ in row_ids)
        sql = f"""SELECT id, layer, content, created_at, metadata, lifecycle_state
                  FROM memory_items
                  WHERE rowid IN ({id_placeholders}) AND archived = 0"""
        params: list[Any] = list(row_ids)

        if layers:
            placeholders = ",".join("?" for _ in layers)
            sql += f" AND layer IN ({placeholders})"
            params.extend(layers)

        rows = conn.execute(sql, params).fetchall()

        # Map back to vector distance scores
        id_to_row: dict[int, sqlite3.Row] = {}
        for row in rows:
            key = None
            # Find rowid from the items
            r = conn.execute("SELECT rowid FROM memory_items WHERE id = ?", (row["id"],)).fetchone()
            if r:
                key = r[0]
            if key is not None:
                id_to_row[key] = row

        results = []
        for rid in row_ids:
            row = id_to_row.get(rid)
            if row is None:
                continue
            dist = distances[rid]
            # Convert cosine distance to similarity score (0..1)
            similarity = max(0.0, 1.0 - dist)
            meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else {}
            results.append({
                "id": row["id"],
                "layer": row["layer"],
                "content": row["content"],
                "score": similarity,
                "_vec_score": similarity,
                "metadata": meta,
                "created_at": row["created_at"],
            })

        return results

    # ── Knowledge graph (L2) ───────────────────────────────────────

    def save_relationship(
        self,
        content: str,
        people: list[str] | None = None,
        skills: list[str] | None = None,
        source: str = "manual",
    ) -> str:
        """Save a relationship memory entry. Creates/merges nodes and edges."""
        mem_id = self.save_memory("L2", content, {"source": source, "people": people or [], "skills": skills or []})

        conn = self._get_conn()
        me_name = os.environ.get("ME_NAME", "agent")
        now = datetime.now(timezone.utc).isoformat()

        # Ensure self node
        conn.execute(
            "INSERT OR IGNORE INTO graph_nodes (id, type, name) VALUES (?, 'person', ?)",
            (me_name, me_name),
        )

        # Create person nodes and KNOWS edges
        for person in (people or []):
            if not person:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO graph_nodes (id, type, name) VALUES (?, 'person', ?)",
                (person, person),
            )
            conn.execute(
                "INSERT OR IGNORE INTO graph_edges (source_id, target_id, relationship, created_at) VALUES (?, ?, 'KNOWS', ?)",
                (me_name, person, now),
            )

        # Create skill nodes and HAS_SKILL edges
        for skill in (skills or []):
            if not skill:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO graph_nodes (id, type, name) VALUES (?, 'skill', ?)",
                (skill, skill),
            )
            conn.execute(
                "INSERT OR IGNORE INTO graph_edges (source_id, target_id, relationship, created_at) VALUES (?, ?, 'HAS_SKILL', ?)",
                (me_name, skill, now),
            )

        conn.commit()
        return mem_id

    def query_graph(self, query_text: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Search graph nodes and relationships.

        Uses recursive CTE for relationship traversal + FTS fallback on content.
        """
        # First try: match by name
        conn = self._get_conn()
        q = f"%{query_text}%"
        rows = conn.execute(
            """SELECT id, type, name, metadata
               FROM graph_nodes
               WHERE name LIKE ?
               LIMIT ?""",
            (q, top_k),
        ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            # Get relationships for this node (one hop)
            edges = conn.execute(
                """SELECT e.relationship, e.source_id, e.target_id,
                          n2.name AS related_name, n2.type AS related_type
                   FROM graph_edges e
                   JOIN graph_nodes n2 ON n2.id = CASE WHEN e.source_id = ? THEN e.target_id ELSE e.source_id END
                   WHERE e.source_id = ? OR e.target_id = ?
                   LIMIT 20""",
                (row["id"], row["id"], row["id"]),
            ).fetchall()

            rels = [
                {
                    "relationship": e["relationship"],
                    "related_name": e["related_name"],
                    "related_type": e["related_type"],
                }
                for e in edges
            ]

            meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else {}
            results.append({
                "id": row["id"],
                "type": row["type"],
                "name": row["name"],
                "metadata": meta,
                "relationships": rels,
                "score": 0.8,
            })

        # Second try: match memory content via FTS
        if not results or len(results) < top_k:
            mem_results = self._recall_fts(query_text, ["L2"], top_k - len(results))
            for mr in mem_results:
                results.append({
                    "id": mr["id"],
                    "type": "memory",
                    "name": mr["content"][:80],
                    "metadata": mr["metadata"],
                    "relationships": [],
                    "score": mr["score"],
                })

        return results[:top_k]

    def graph_stats(self) -> dict[str, int]:
        conn = self._get_conn()
        nodes = conn.execute("SELECT COUNT(*) AS c FROM graph_nodes").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) AS c FROM graph_edges").fetchone()[0]
        return {"nodes": nodes, "relationships": edges}

    # ── Sessions (L1) ──────────────────────────────────────────────

    def record_session(self, agent_name: str, status: str = "active", summary: str = "") -> str:
        now = datetime.now(timezone.utc).isoformat()
        session_id = f"SES-{uuid.uuid4().hex[:12].upper()}"
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO sessions (id, agent_name, status, summary, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, agent_name, status, summary, now, now),
        )
        conn.commit()
        return session_id

    def list_sessions(self, recent: int = 5) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT id, agent_name, status, summary, created_at, updated_at
               FROM sessions ORDER BY created_at DESC LIMIT ?""",
            (recent,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Hardening rules (L0) ───────────────────────────────────────

    def save_rule(
        self,
        rule_id: str,
        trigger: str,
        pattern: str,
        corrective: str,
        level: str = "soft",
        source_decision_id: str = "",
        evidence_count: int = 1,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO hardening_rules
               (id, trigger, pattern, corrective, level, enabled, source_decision_id, evidence_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (rule_id, trigger, pattern, corrective, level, source_decision_id, evidence_count, now, now),
        )
        conn.commit()
        return self.get_rule(rule_id)

    def get_rule(self, rule_id: str) -> dict[str, Any]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM hardening_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if row is None:
            return {"id": rule_id, "found": False}
        return dict(row)

    def list_rules(self, enabled: bool | None = None) -> list[dict[str, Any]]:
        conn = self._get_conn()
        if enabled is not None:
            rows = conn.execute(
                "SELECT * FROM hardening_rules WHERE enabled = ? ORDER BY created_at DESC",
                (1 if enabled else 0,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM hardening_rules ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_rule_enabled(self, rule_id: str, enabled: bool, reason: str = "") -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        conn.execute(
            "UPDATE hardening_rules SET enabled = ?, disabled_reason = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, reason, now, rule_id),
        )
        conn.commit()
        return self.get_rule(rule_id)

    # ── Health & stats ─────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        conn = self._get_conn()
        try:
            conn.execute("SELECT 1")
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        return {
            "status": "ok",
            "db_path": self.db_path,
            "db_size": Path(self.db_path).stat().st_size if Path(self.db_path).exists() else 0,
            "embedding_available": _EMBEDDER.available,
        }

    def memory_stats(self) -> dict[str, int]:
        conn = self._get_conn()
        stats: dict[str, int] = {}
        for layer in ("L2", "L3", "L4", "L5", "L6"):
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_items WHERE layer = ? AND archived = 0", (layer,)
            ).fetchone()
            stats[f"{layer.lower()}_count"] = row[0]
        stats["total"] = sum(stats.values())

        row = conn.execute("SELECT COUNT(*) AS c FROM graph_nodes").fetchone()
        stats["graph_nodes"] = row[0]
        row = conn.execute("SELECT COUNT(*) AS c FROM graph_edges").fetchone()
        stats["graph_edges"] = row[0]
        row = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()
        stats["sessions"] = row[0]
        row = conn.execute("SELECT COUNT(*) AS c FROM hardening_rules").fetchone()
        stats["rules"] = row[0]

        stats["db_size"] = Path(self.db_path).stat().st_size if Path(self.db_path).exists() else 0
        return stats

    def compact(self, dry_run: bool = True) -> dict[str, Any]:
        """Archive old sessions (>30d) and clean up decayed items."""
        conn = self._get_conn()
        report: dict[str, Any] = {"dry_run": dry_run, "archived_sessions": 0, "archived_memories": 0}

        threshold = datetime.now(timezone.utc).timestamp() - (30 * 86400)
        threshold_iso = datetime.fromtimestamp(threshold, tz=timezone.utc).isoformat()

        # Archive old sessions
        old_sessions = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE created_at < ?",
            (threshold_iso,),
        ).fetchone()[0]
        report["archived_sessions"] = old_sessions
        if not dry_run and old_sessions:
            conn.execute("DELETE FROM sessions WHERE created_at < ?", (threshold_iso,))

        # Mark decayed items as archived
        decayed = conn.execute(
            "SELECT COUNT(*) AS c FROM memory_items WHERE decay_score >= 1.0 AND archived = 0",
        ).fetchone()[0]
        report["archived_memories"] = decayed
        if not dry_run and decayed:
            conn.execute(
                "UPDATE memory_items SET archived = 1, lifecycle_state = 'archived' WHERE decay_score >= 1.0 AND archived = 0",
            )

        # VACUUM in non-dry-run to reclaim space
        if not dry_run:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")

        conn.commit()
        report["db_size_after"] = Path(self.db_path).stat().st_size if Path(self.db_path).exists() else 0
        return report


# ── Module-level convenience ──────────────────────────────────────

_default_store: SQLiteStore | None = None
_store_lock = threading.Lock()


def get_store(db_path: str | None = None) -> SQLiteStore:
    """Get or create the default SQLiteStore singleton."""
    global _default_store
    if _default_store is None:
        with _store_lock:
            if _default_store is None:
                _default_store = SQLiteStore(db_path)
    return _default_store
