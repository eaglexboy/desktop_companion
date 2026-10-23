"""SQLite-backed personality state: slow-drifting traits (who it is),
fast-decaying mood (how it's doing right now), a running memory-notes log
(things learned about the user/itself), a short interaction log that
brain/personality/reflect.py consumes periodically and marks done, and a
generic agent_state KV table reserved for future voice/appearance-evolution
accommodations.

Deliberately synchronous (sqlite3, no ORM) — this is small, local, and fast
enough that a simple blocking implementation is the right amount of
complexity here. Callers on the async side wrap calls in
``asyncio.to_thread`` where it matters (see brain/main.py).

The ``interactions`` table has a ``reflected`` column: nothing else in the
schema can distinguish "already reflected on" from "pending," and without
it ``reflect.py``'s whole "only look at new interactions" design has no way
to work at all.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from brain.events import emit

NEUTRAL_TRAIT = 50.0
NEUTRAL_MOOD = 50.0
# Placeholder default — the agent (or the user) can rename it later via
# set_identity_name(); every subsequent prompt build reads it back from here,
# so a chosen name actually sticks instead of resetting each call.
DEFAULT_NAME = "Deskmate"
_NAME_KEY = "name"

# Canonical agent_state keys -- reserved for future features (autonomous
# voice selection, avatar evolution staging). Never pre-seeded:
# get_agent_state() returns None until something actually writes one, so
# adding new state fields never requires a DDL migration.
AGENT_STATE_KEYS = (
    "current_voice_id",
    "previous_voice_id",
    "staged_voice_id",
    "voice_pinned",
    "staged_version",
    "last_redesign_timestamp",
    "base_template_since",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traits (
    name TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 50.0
);
CREATE TABLE IF NOT EXISTS mood (
    name TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 50.0,
    baseline REAL NOT NULL DEFAULT 50.0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS identity (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    role TEXT NOT NULL,          -- 'user' | 'assistant'
    text TEXT NOT NULL,
    provider TEXT,                -- which LLM server produced it (assistant turns)
    escalated INTEGER NOT NULL DEFAULT 0,
    reflected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- Time series of mood/trait values, for the status page's timeline. Written
-- on every deliberate change, and otherwise only when the value has visibly
-- drifted (decay) or a few minutes have passed, so it stays small.
CREATE TABLE IF NOT EXISTS personality_history (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,           -- 'mood' | 'traits'
    name TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_personality_history_ts ON personality_history(kind, ts);
"""

HISTORY_RETENTION_S = 30 * 24 * 3600.0
_HISTORY_MIN_DRIFT = 0.25
_HISTORY_MAX_GAP_S = 300.0


@dataclass
class MemoryNote:
    id: int
    ts: float
    note: str


class PersonalityStore:
    def __init__(
        self,
        db_path: Path,
        trait_names: tuple[str, ...],
        mood_names: tuple[str, ...],
        identity_name: str = DEFAULT_NAME,
    ) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._seed(trait_names, mood_names, identity_name)
        self._conn.commit()
        self._last_snapshot: dict[str, tuple[float, dict[str, float]]] = {}
        with self._conn:
            self._conn.execute("DELETE FROM personality_history WHERE ts < ?", (time.time() - HISTORY_RETENTION_S,))
        self._record("mood", self.get_mood(), {}, "startup")
        self._record("traits", self.get_traits(), {}, "startup")

    def _record(self, kind: str, values: dict[str, float], deltas: dict[str, float], cause: str) -> None:
        """Announce a change on the event bus and, if it is worth a point on
        the timeline, persist it. Never raises: observation must not be able
        to break a mood update."""
        try:
            now = time.time()
            last = self._last_snapshot.get(kind)
            drifted = last is None or any(abs(v - last[1].get(n, v)) >= _HISTORY_MIN_DRIFT for n, v in values.items())
            if cause != "decay" or drifted or now - last[0] >= _HISTORY_MAX_GAP_S:
                # Routine decay is announced only when it is also worth a point
                # on the timeline; per-minute drift of a few hundredths isn't.
                emit(f"personality.{kind}", {"values": values, "deltas": deltas, "cause": cause})
                with self._conn:
                    self._conn.executemany(
                        "INSERT INTO personality_history(ts, kind, name, value) VALUES (?, ?, ?, ?)",
                        [(now, kind, n, v) for n, v in values.items()],
                    )
                self._last_snapshot[kind] = (now, dict(values))
        except Exception:  # noqa: BLE001
            pass

    def _seed(self, trait_names: tuple[str, ...], mood_names: tuple[str, ...], identity_name: str) -> None:
        now = time.time()
        with self._conn:
            for name in trait_names:
                self._conn.execute(
                    "INSERT OR IGNORE INTO traits(name, value) VALUES (?, ?)", (name, NEUTRAL_TRAIT)
                )
            for name in mood_names:
                self._conn.execute(
                    "INSERT OR IGNORE INTO mood(name, value, baseline, updated_at) VALUES (?, ?, ?, ?)",
                    (name, NEUTRAL_MOOD, NEUTRAL_MOOD, now),
                )
            # INSERT OR IGNORE: only takes effect on a brand-new database.
            # Seeding (not UPSERT-ing) means a later autonomous self-rename
            # (set_identity_name()) persists across restarts instead of
            # being stomped back to whatever config.toml currently says.
            self._conn.execute(
                "INSERT OR IGNORE INTO identity(key, value) VALUES (?, ?)", (_NAME_KEY, identity_name)
            )

    # --- identity ---
    def get_identity_name(self) -> str:
        row = self._conn.execute("SELECT value FROM identity WHERE key = ?", (_NAME_KEY,)).fetchone()
        return row["value"] if row else DEFAULT_NAME

    def set_identity_name(self, name: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO identity(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_NAME_KEY, name),
            )

    # --- traits (slow) ---
    def get_traits(self) -> dict[str, float]:
        rows = self._conn.execute("SELECT name, value FROM traits").fetchall()
        return {row["name"]: row["value"] for row in rows}

    def adjust_traits(self, deltas: dict[str, float], cause: str = "") -> dict[str, float]:
        """Apply small deltas, clamped to [0, 100]. Returns the new values."""
        with self._conn:
            for name, delta in deltas.items():
                self._conn.execute(
                    "UPDATE traits SET value = MAX(0, MIN(100, value + ?)) WHERE name = ?",
                    (delta, name),
                )
        values = self.get_traits()
        self._record("traits", values, deltas, cause or "adjust")
        return values

    # --- mood (fast, decays toward its own per-row baseline over time) ---
    def get_mood(self) -> dict[str, float]:
        rows = self._conn.execute("SELECT name, value FROM mood").fetchall()
        return {row["name"]: row["value"] for row in rows}

    def nudge_mood(self, deltas: dict[str, float], cause: str = "") -> dict[str, float]:
        now = time.time()
        with self._conn:
            for name, delta in deltas.items():
                self._conn.execute(
                    "UPDATE mood SET value = MAX(0, MIN(100, value + ?)), updated_at = ? WHERE name = ?",
                    (delta, now, name),
                )
        values = self.get_mood()
        self._record("mood", values, deltas, cause or "nudge")
        return values

    def decay_mood(self, half_life_s: float = 1800.0) -> dict[str, float]:
        """Exponentially relax each mood value toward its own ``baseline``
        column based on elapsed time since it was last touched. Call this
        before reading mood for anything user-facing (idle behavior
        weighting, tone). Baseline is baked into the row rather than passed
        as a runtime argument, so it can't drift out of sync between calls."""
        now = time.time()
        rows = self._conn.execute("SELECT name, value, baseline, updated_at FROM mood").fetchall()
        with self._conn:
            for row in rows:
                elapsed = max(0.0, now - row["updated_at"])
                factor = 0.5 ** (elapsed / half_life_s) if half_life_s > 0 else 0.0
                new_value = row["baseline"] + (row["value"] - row["baseline"]) * factor
                self._conn.execute(
                    "UPDATE mood SET value = ?, updated_at = ? WHERE name = ?", (new_value, now, row["name"])
                )
        values = self.get_mood()
        self._record("mood", values, {}, "decay")
        return values

    # --- memory notes (no weight column -- retrieval is recency + keyword
    # overlap, computed in brain/personality/prompts.py, not stored here) ---
    def add_memory_note(self, note: str) -> None:
        with self._conn:
            self._conn.execute("INSERT INTO memories(ts, note) VALUES (?, ?)", (time.time(), note))

    def recent_memory_notes(self, limit: int = 20) -> list[MemoryNote]:
        rows = self._conn.execute(
            "SELECT id, ts, note FROM memories ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [MemoryNote(row["id"], row["ts"], row["note"]) for row in rows]

    def prune_memory_notes(self, keep: int = 200) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM memories WHERE id NOT IN "
                "(SELECT id FROM memories ORDER BY ts DESC LIMIT ?)",
                (keep,),
            )

    # --- interaction log (feeds reflect.py) ---
    def log_interaction(self, role: str, text: str, *, provider: str | None = None, escalated: bool = False) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO interactions(ts, role, text, provider, escalated) VALUES (?, ?, ?, ?, ?)",
                (time.time(), role, text, provider, int(escalated)),
            )

    def pending_interactions(self, limit: int = 200) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM interactions WHERE reflected = 0 ORDER BY ts ASC LIMIT ?", (limit,)
        ).fetchall()

    def mark_reflected(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._conn:
            self._conn.executemany("UPDATE interactions SET reflected = 1 WHERE id = ?", [(i,) for i in ids])

    # --- agent_state (generic KV, V2+ accommodation -- see AGENT_STATE_KEYS) ---
    def get_agent_state(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM agent_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_agent_state(self, key: str, value: str | None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO agent_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def close(self) -> None:
        self._conn.close()
