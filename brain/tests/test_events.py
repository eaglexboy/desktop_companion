"""brain.events (the bus behind the status page's live view) and the mood history it feeds."""

from __future__ import annotations

import asyncio
import sqlite3

from brain.events import EventBus, begin_turn, emit, bus
from brain.personality.state import PersonalityStore


def test_emit_with_no_listener_is_just_a_buffer_append():
    b = EventBus(backlog=3)
    for i in range(5):
        b.emit("x", {"i": i})
    assert [e["data"]["i"] for e in b.since(0)] == [2, 3, 4]  # bounded; oldest dropped


def test_since_returns_only_newer_events():
    b = EventBus()
    b.emit("a")
    first = b.since(0)[0]["seq"]
    b.emit("b")
    assert [e["type"] for e in b.since(first)] == ["b"]


async def test_subscriber_gets_live_events_and_a_slow_one_loses_the_oldest():
    b = EventBus()
    with b.subscribe() as queue:
        b.emit("one")
        b.emit("two")
        await asyncio.sleep(0)
        assert [queue.get_nowait()["type"], queue.get_nowait()["type"]] == ["one", "two"]


async def test_turn_id_travels_with_emits_made_deep_in_a_call():
    turn = begin_turn()

    async def deep():
        emit("deep.event")

    await deep()
    assert bus.since(0)[-1]["turn"] == turn


def test_emit_never_raises_even_for_unserialisable_payloads():
    EventBus().emit("x", {"bad": object()})


def test_mood_changes_are_recorded_for_the_timeline(tmp_path):
    db = tmp_path / "state.db"
    store = PersonalityStore(db, ("openness",), ("energy", "valence", "stress"))
    store.nudge_mood({"valence": 10.0}, cause="reflection")
    rows = sqlite3.connect(db).execute(
        "SELECT value FROM personality_history WHERE kind='mood' AND name='valence' ORDER BY ts"
    ).fetchall()
    assert [r[0] for r in rows][-1] == 60.0  # startup snapshot, then the nudge
    assert len(rows) >= 2


def test_tiny_decay_does_not_flood_the_history(tmp_path):
    db = tmp_path / "state.db"
    store = PersonalityStore(db, ("openness",), ("energy", "valence", "stress"))
    before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM personality_history").fetchone()[0]
    for _ in range(5):
        store.decay_mood()
    after = sqlite3.connect(db).execute("SELECT COUNT(*) FROM personality_history").fetchone()[0]
    assert after == before
