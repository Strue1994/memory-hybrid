"""SQLite-backed unified storage layer for memory-hybrid.

Replaces file-scattered storage with a single SQLite DB that provides:
- sqlite-vec vector search (true KNN, SIMD accelerated)
- FTS5 full-text search (BM25 ranking)
- Graph traversal via recursive CTEs + adjacency table
- ACID transactions via WAL mode
- Single-file backup (cp memory.db backup.db)
"""

from .sqlite_store import SQLiteStore
from .file_mirror import FileMirror

__all__ = ["SQLiteStore", "FileMirror"]
