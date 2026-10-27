"""Which installed Agent Skills are enabled and pinned (`skill_state` in state.db).

Three independent facts per skill (docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md,
"Skill Activation & Context Budget"): installed (on disk), enabled (eligible to
be offered), pinned (always shown as a one-line stub). This module only stores
them; the rules about who may change them live in brain/skills/manage.py.

It keeps its own connection to the same database file as the personality
store: SQLite handles the two, and neither module needs to know the other.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_state (
    skill_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    alias TEXT,
    changed_by TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class SkillState:
    skill_id: str
    enabled: bool
    pinned: bool
    alias: str | None
    changed_by: str
    updated_at: float


class SkillStateStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    @staticmethod
    def _row(r: sqlite3.Row) -> SkillState:
        return SkillState(r["skill_id"], bool(r["enabled"]), bool(r["pinned"]), r["alias"], r["changed_by"], r["updated_at"])

    def get(self, skill_id: str) -> SkillState | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM skill_state WHERE skill_id = ?", (skill_id,)).fetchone()
        return self._row(r) if r else None

    def all(self) -> dict[str, SkillState]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM skill_state").fetchall()
        return {r["skill_id"]: self._row(r) for r in rows}

    def put(self, skill_id: str, *, enabled: bool, pinned: bool, alias: str | None, changed_by: str) -> SkillState:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO skill_state(skill_id, enabled, pinned, alias, changed_by, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(skill_id) DO UPDATE SET enabled=excluded.enabled, pinned=excluded.pinned, "
                "alias=excluded.alias, changed_by=excluded.changed_by, updated_at=excluded.updated_at",
                (skill_id, int(enabled), int(pinned), alias, changed_by, time.time()),
            )
        state = self.get(skill_id)
        assert state is not None
        return state

    def close(self) -> None:
        self._conn.close()
