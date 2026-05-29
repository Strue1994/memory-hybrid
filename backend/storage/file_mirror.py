"""Optional file mirror — writes human-readable .md/.yaml alongside SQLite.

Keeps existing file-reading workflows working while the source of truth
moves to SQLite. Disabled by default; set MIRROR_FILES=true to enable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def _enabled() -> bool:
    return os.environ.get("MIRROR_FILES", "").lower() in ("1", "true", "yes")


def _memory_root() -> Path:
    env = os.environ.get("MEMORY_HYBRID_ROOT") or os.environ.get("MEMORY_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


class FileMirror:
    """Writes human-readable file mirrors alongside SQLite storage.

    Only writes when MIRROR_FILES=true. Silent no-op otherwise.
    """

    def __init__(self) -> None:
        self.root = _memory_root()
        self.enabled = _enabled()

    def on_memory_saved(self, layer: str, content: str, metadata: dict[str, Any], memory_id: str) -> None:
        if not self.enabled:
            return
        now = datetime.now(timezone.utc)
        if layer == "L3":
            self._write_timeline(content, metadata, now)
        elif layer == "L4":
            self._write_fact(content, metadata, memory_id, now)
        elif layer == "L5":
            self._write_goal(content, metadata, now)
        elif layer == "L6":
            self._write_decision(content, metadata, memory_id, now)

    def on_session_recorded(self, agent_name: str, summary: str, session_id: str) -> None:
        if not self.enabled:
            return
        now = datetime.now(timezone.utc)
        sessions_dir = self.root / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        path = sessions_dir / f"SESSION-{now.strftime('%Y%m%dT%H%M%SZ')}-{agent_name}.md"
        lines = [
            f"# Session: {agent_name}",
            f"- session_id: {session_id}",
            f"- timestamp: {now.isoformat()}",
            f"- agent: {agent_name}",
            f"- summary: {summary}",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_timeline(self, content: str, metadata: dict[str, Any], now: datetime) -> None:
        day_dir = self.root / "timeline" / now.strftime("%Y") / now.strftime("%m")
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{now.strftime('%Y-%m-%d')}.md"
        entry = [
            f"- {now.strftime('%H:%M:%S')} | {metadata.get('event_type', 'work')}",
            f"  {content}",
        ]
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(entry) + "\n")

    def _write_fact(self, content: str, metadata: dict[str, Any], memory_id: str, now: datetime) -> None:
        facts_dir = self.root / "facts" / "curated"
        facts_dir.mkdir(parents=True, exist_ok=True)
        path = facts_dir / f"{memory_id[:12]}.md"
        path.write_text(
            f"# Fact: {metadata.get('category', 'general')}\n"
            f"- id: {memory_id}\n"
            f"- created: {now.isoformat()}\n"
            f"- source: {metadata.get('source', 'manual')}\n"
            f"- importance: {metadata.get('importance', 3)}\n"
            f"- verified: {metadata.get('verified', False)}\n\n"
            f"{content}\n",
            encoding="utf-8",
        )

    def _write_goal(self, content: str, metadata: dict[str, Any], now: datetime) -> None:
        goals_file = self.root / "goals" / "active.yaml"
        goals_file.parent.mkdir(parents=True, exist_ok=True)
        goal_entry = {
            "id": metadata.get("goal_id", ""),
            "title": metadata.get("title", "") or content[:60],
            "priority": metadata.get("priority", "medium"),
            "status": metadata.get("status", "not_started"),
            "progress": metadata.get("progress", 0),
            "deadline": metadata.get("deadline", ""),
            "created_at": now.isoformat(),
            "content": content,
        }
        existing = {"goals": []}
        if goals_file.exists():
            try:
                existing = yaml.safe_load(goals_file.read_text(encoding="utf-8")) or {"goals": []}
            except Exception:
                existing = {"goals": []}
        existing.setdefault("goals", []).append(goal_entry)
        goals_file.write_text(yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _write_decision(self, content: str, metadata: dict[str, Any], memory_id: str, now: datetime) -> None:
        dec_dir = self.root / "decisions" / now.strftime("%Y") / now.strftime("%m")
        dec_dir.mkdir(parents=True, exist_ok=True)
        path = dec_dir / f"{now.strftime('%Y-%m-%d')}-{memory_id[:12]}.md"
        path.write_text(
            f"# Decision: {metadata.get('decision_id', memory_id[:12])}\n"
            f"- id: {memory_id}\n"
            f"- timestamp: {now.isoformat()}\n"
            f"- trigger: {metadata.get('trigger', '')}\n"
            f"- metadata: {json.dumps(metadata, ensure_ascii=False)}\n\n"
            f"{content}\n",
            encoding="utf-8",
        )

    def on_rules_changed(self, rules: list[dict[str, Any]]) -> None:
        if not self.enabled:
            return
        path = self.root / "hardening" / "rules.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"rules": rules}, allow_unicode=True, sort_keys=False), encoding="utf-8")
