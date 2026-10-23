"""Tests for brain.personality.reflect: run_reflection() and the Trigger-B
autonomous-redesign eligibility gate."""

from __future__ import annotations

import json

import pytest

from brain.config import MOOD_NAMES, TRAIT_NAMES
from brain.llm_pool import LLMClient, LLMPool, Message
from brain.personality.reflect import maybe_trigger_autonomous_redesign, run_reflection
from brain.personality.state import PersonalityStore


class _ScriptedClient(LLMClient):
    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def chat(self, messages: list[Message], *, max_tokens: int = 1024, **kwargs) -> str:
        return self._reply


def _pool(reply: str) -> LLMPool:
    return LLMPool([("local-1", _ScriptedClient(reply), [])])


def _store(tmp_path):
    return PersonalityStore(tmp_path / "p.db", TRAIT_NAMES, MOOD_NAMES)


# --- run_reflection ----------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_applies_capped_deltas_and_notes(tmp_path):
    store = _store(tmp_path)
    store.log_interaction("user", "let's try something new today")
    store.log_interaction("assistant", "sure, how about X", provider="local-1", escalated=False)

    # deltas here exceed the caps on purpose, to prove they get clamped
    reply = json.dumps(
        {
            "trait_deltas": {"openness": 999, "boldness": -999},
            "mood_deltas": {"energy": 999},
            "memory_notes": ["user likes trying new things"],
        }
    )
    result = await run_reflection(store, _pool(reply))
    assert result.changed is True
    assert result.trait_deltas == {"openness": 5.0, "boldness": -5.0}
    assert result.mood_deltas == {"energy": 20.0}
    assert result.notes_created == 1

    traits = store.get_traits()
    assert traits["openness"] == 55.0  # 50 + capped +5
    assert traits["boldness"] == 45.0  # 50 - capped 5
    assert store.get_mood()["energy"] == 70.0  # 50 + capped +20
    assert store.pending_interactions() == []
    notes = [n.note for n in store.recent_memory_notes()]
    assert "user likes trying new things" in notes


@pytest.mark.asyncio
async def test_reflection_ignores_unknown_trait_and_mood_names(tmp_path):
    store = _store(tmp_path)
    store.log_interaction("user", "hi")
    reply = json.dumps({"trait_deltas": {"not_a_real_trait": 5}, "mood_deltas": {"not_a_real_mood": 10}})
    result = await run_reflection(store, _pool(reply))
    assert result.changed is True
    assert result.trait_deltas == {}
    assert result.mood_deltas == {}
    # nothing moved -- the bogus names were filtered out, not applied
    assert store.get_traits()["openness"] == 50.0
    assert store.get_mood()["energy"] == 50.0


@pytest.mark.asyncio
async def test_reflection_noop_when_nothing_pending(tmp_path):
    store = _store(tmp_path)
    result = await run_reflection(store, _pool("{}"))
    assert result.changed is False
    assert bool(result) is False


@pytest.mark.asyncio
async def test_reflection_noop_when_local_pool_empty(tmp_path):
    store = _store(tmp_path)
    store.log_interaction("user", "hi")
    result = await run_reflection(store, LLMPool([]))
    assert result.changed is False
    assert len(store.pending_interactions()) == 1  # left pending for a later retry


@pytest.mark.asyncio
async def test_reflection_survives_garbage_llm_output(tmp_path):
    store = _store(tmp_path)
    store.log_interaction("user", "hi")
    result = await run_reflection(store, _pool("not json at all"))
    assert result.changed is False
    # interaction stays pending since the pass failed, so a later retry can pick it up
    assert len(store.pending_interactions()) == 1


# --- maybe_trigger_autonomous_redesign (Trigger B) ---------------------------


def _gate_kwargs(**overrides):
    defaults = {
        "is_build_running": lambda: False,
        "is_user_interacting": lambda: False,
        "redesign_cooldown_hours_min": 12.0,
        "redesign_cooldown_hours_max": 24.0,
        "autonomous_redesign_probability": 1.0,  # always wins the roll by default, for deterministic tests
    }
    defaults.update(overrides)
    return defaults


@pytest.mark.asyncio
async def test_trigger_b_fires_when_never_redesigned_before_and_roll_succeeds(tmp_path):
    store = _store(tmp_path)
    called = []

    async def on_eligible():
        called.append(True)

    fired = await maybe_trigger_autonomous_redesign(store, on_eligible=on_eligible, **_gate_kwargs())

    assert fired is True
    assert called == [True]


@pytest.mark.asyncio
async def test_trigger_b_skipped_when_build_running(tmp_path):
    store = _store(tmp_path)
    called = []

    async def on_eligible():
        called.append(True)

    fired = await maybe_trigger_autonomous_redesign(
        store, on_eligible=on_eligible, **_gate_kwargs(is_build_running=lambda: True)
    )

    assert fired is False
    assert called == []


@pytest.mark.asyncio
async def test_trigger_b_skipped_when_user_interacting(tmp_path):
    store = _store(tmp_path)
    called = []

    async def on_eligible():
        called.append(True)

    fired = await maybe_trigger_autonomous_redesign(
        store, on_eligible=on_eligible, **_gate_kwargs(is_user_interacting=lambda: True)
    )

    assert fired is False
    assert called == []


@pytest.mark.asyncio
async def test_trigger_b_skipped_when_cooldown_not_elapsed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.set_agent_state("last_redesign_timestamp", str(__import__("time").time()))  # just now
    monkeypatch.setattr("brain.personality.reflect.random.uniform", lambda a, b: a)  # shortest possible cooldown
    called = []

    async def on_eligible():
        called.append(True)

    fired = await maybe_trigger_autonomous_redesign(store, on_eligible=on_eligible, **_gate_kwargs())

    assert fired is False  # 0 hours elapsed < even the shortest 12h threshold
    assert called == []


@pytest.mark.asyncio
async def test_trigger_b_fires_once_cooldown_has_elapsed(tmp_path, monkeypatch):
    import time as time_module

    store = _store(tmp_path)
    store.set_agent_state("last_redesign_timestamp", str(time_module.time() - 100 * 3600))  # 100h ago
    monkeypatch.setattr("brain.personality.reflect.random.uniform", lambda a, b: a)
    called = []

    async def on_eligible():
        called.append(True)

    fired = await maybe_trigger_autonomous_redesign(store, on_eligible=on_eligible, **_gate_kwargs())

    assert fired is True
    assert called == [True]


@pytest.mark.asyncio
async def test_trigger_b_skipped_when_probability_roll_fails(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr("brain.personality.reflect.random.random", lambda: 0.99)
    called = []

    async def on_eligible():
        called.append(True)

    fired = await maybe_trigger_autonomous_redesign(
        store, on_eligible=on_eligible, **_gate_kwargs(autonomous_redesign_probability=0.5)
    )

    assert fired is False
    assert called == []
