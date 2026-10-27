"""Tests for brain.personality.state.PersonalityStore."""

from __future__ import annotations

import time

from brain.config import MOOD_NAMES, TRAIT_NAMES
from brain.personality.state import AGENT_STATE_KEYS, PersonalityStore

TRAITS = ("openness", "warmth_placeholder")  # small subset is fine for most of these tests
MOODS = ("energy", "valence")


def _store(tmp_path):
    return PersonalityStore(tmp_path / "personality.db", TRAITS, MOODS)


# --- traits / mood --------------------------------------------------------


def test_seeds_neutral_traits_and_mood(tmp_path):
    store = _store(tmp_path)
    assert store.get_traits() == {"openness": 50.0, "warmth_placeholder": 50.0}
    assert store.get_mood() == {"energy": 50.0, "valence": 50.0}


def test_adjust_traits_clamped(tmp_path):
    store = _store(tmp_path)
    store.adjust_traits({"openness": 60})
    assert store.get_traits()["openness"] == 100.0  # clamped at 100
    store.adjust_traits({"openness": -1000})
    assert store.get_traits()["openness"] == 0.0  # clamped at 0


def test_mood_decays_toward_its_own_row_baseline(tmp_path):
    """baseline lives in the mood row itself, not a runtime argument to
    decay_mood()."""
    store = _store(tmp_path)
    store.nudge_mood({"energy": 40})  # now 90
    assert store.get_mood()["energy"] == 90.0

    # fake an old updated_at so decay has something to do without sleeping
    with store._conn:
        store._conn.execute("UPDATE mood SET updated_at = ? WHERE name = 'energy'", (time.time() - 1800,))
    decayed = store.decay_mood(half_life_s=1800.0)
    # one half-life elapsed -> halfway back to the row's own baseline (50): 50 + (90-50)*0.5 = 70
    assert abs(decayed["energy"] - 70.0) < 0.5


def test_mood_baseline_defaults_to_neutral_and_is_per_row(tmp_path):
    store = _store(tmp_path)
    rows = store._conn.execute("SELECT name, baseline FROM mood").fetchall()
    assert {row["name"]: row["baseline"] for row in rows} == {"energy": 50.0, "valence": 50.0}


# --- real trait/mood constants ---------------------------------------------


def test_real_trait_and_mood_constants_seed_correctly(tmp_path):
    store = PersonalityStore(tmp_path / "real.db", TRAIT_NAMES, MOOD_NAMES)
    traits = store.get_traits()
    assert len(traits) == 13
    assert traits["honesty_humility"] == 50.0
    assert store.get_mood() == {name: 50.0 for name in MOOD_NAMES}


# --- memory notes (no weight column -- recency only; keyword-overlap
# ranking happens in prompts.py, not here) --------------------------------


def test_memory_notes_ordering_and_prune(tmp_path):
    store = _store(tmp_path)
    store.add_memory_note("likes dark mode")
    store.add_memory_note("hates being interrupted")
    notes = store.recent_memory_notes(limit=10)
    assert [n.note for n in notes] == ["hates being interrupted", "likes dark mode"]  # most recent first
    store.prune_memory_notes(keep=1)
    assert len(store.recent_memory_notes(limit=10)) == 1


# --- interaction log / reflection cursor ----------------------------------


def test_interaction_log_reflection_cycle(tmp_path):
    store = _store(tmp_path)
    store.log_interaction("user", "hi")
    store.log_interaction("assistant", "hello!", provider="local-1", escalated=False)
    pending = store.pending_interactions()
    assert len(pending) == 2
    store.mark_reflected([row["id"] for row in pending])
    assert store.pending_interactions() == []


def test_log_interaction_escalated_defaults_false_not_null(tmp_path):
    """escalated is NOT NULL DEFAULT 0 -- confirm user turns get a concrete
    0 rather than NULL."""
    store = _store(tmp_path)
    store.log_interaction("user", "hi")
    row = store.pending_interactions()[0]
    assert row["escalated"] == 0


# --- identity --------------------------------------------------------------


def test_identity_name_defaults_and_persists(tmp_path):
    store = _store(tmp_path)
    assert store.get_identity_name() == "Deskmate"
    store.set_identity_name("Bob")
    assert store.get_identity_name() == "Bob"


def test_identity_name_seeds_from_config_on_fresh_install(tmp_path):
    store = PersonalityStore(tmp_path / "personality.db", TRAITS, MOODS, identity_name="Aria")
    assert store.get_identity_name() == "Aria"


def test_identity_name_config_value_does_not_stomp_an_existing_rename(tmp_path):
    db_path = tmp_path / "personality.db"
    store = PersonalityStore(db_path, TRAITS, MOODS, identity_name="Aria")
    store.set_identity_name("Bob")  # the agent (or user) renamed it at runtime
    store.close()

    # reopening with a *different* configured identity_name must not reset
    # the already-persisted name -- INSERT OR IGNORE only applies on a
    # brand-new database.
    reopened = PersonalityStore(db_path, TRAITS, MOODS, identity_name="Something Else")
    assert reopened.get_identity_name() == "Bob"


# --- agent_state (net-new KV table, V2+ accommodation) ---------------------


def test_agent_state_unset_key_returns_none(tmp_path):
    store = _store(tmp_path)
    for key in AGENT_STATE_KEYS:
        assert store.get_agent_state(key) is None


def test_agent_state_round_trip(tmp_path):
    store = _store(tmp_path)
    store.set_agent_state("current_voice_id", "piper:en_US-hfc_female-medium")
    assert store.get_agent_state("current_voice_id") == "piper:en_US-hfc_female-medium"
    # overwrite
    store.set_agent_state("current_voice_id", "remote:voxcpm")
    assert store.get_agent_state("current_voice_id") == "remote:voxcpm"
    # untouched keys stay unset
    assert store.get_agent_state("staged_version") is None


def test_agent_state_not_pre_seeded(tmp_path):
    """Confirms "lazily populated, no pre-seeding" -- the table should be
    empty right after construction, unlike traits/mood/identity."""
    store = _store(tmp_path)
    rows = store._conn.execute("SELECT COUNT(*) AS n FROM agent_state").fetchone()
    assert rows["n"] == 0


# --- raw model reply kept next to the cleaned interaction text ------------------


def test_log_interaction_returns_the_row_id_and_keeps_the_raw_reply(tmp_path):
    store = _store(tmp_path)
    first = store.log_interaction("user", "hi")
    second = store.log_interaction("assistant", "Hello there.", raw_text='{"say": "*smiles* Hello there."}')

    assert second == first + 1
    row = store._conn.execute(
        "SELECT i.text, r.raw FROM interactions i JOIN reply_raw r ON r.interaction_id = i.id WHERE i.id = ?",
        (second,),
    ).fetchone()
    assert row["text"] == "Hello there."
    assert row["raw"] == '{"say": "*smiles* Hello there."}'


def test_turns_without_a_raw_reply_have_no_reply_raw_row(tmp_path):
    store = _store(tmp_path)
    store.log_interaction("user", "hi")
    store.log_interaction("assistant", "hello")
    assert store._conn.execute("SELECT COUNT(*) FROM reply_raw").fetchone()[0] == 0


def test_a_runaway_raw_reply_is_capped(tmp_path):
    from brain.personality.state import RAW_REPLY_MAX_CHARS

    store = _store(tmp_path)
    interaction_id = store.log_interaction("assistant", "x", raw_text="y" * (RAW_REPLY_MAX_CHARS + 500))
    raw = store._conn.execute("SELECT raw FROM reply_raw WHERE interaction_id = ?", (interaction_id,)).fetchone()[0]
    assert len(raw) == RAW_REPLY_MAX_CHARS


def test_opening_a_database_from_before_reply_raw_adds_the_table(tmp_path):
    """No ALTER needed: CREATE TABLE IF NOT EXISTS is the whole migration."""
    import sqlite3

    store = _store(tmp_path)
    path = tmp_path / "personality.db"
    store.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE reply_raw")
    conn.commit()
    conn.close()

    reopened = _store(tmp_path)
    assert reopened.log_interaction("assistant", "hi", raw_text="raw hi") > 0


def test_state_db_uses_wal(tmp_path):
    import sqlite3
    path = tmp_path / "state.db"
    PersonalityStore(path, ("a",), ("m",))
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"
