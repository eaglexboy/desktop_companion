"""Tests for brain.personality.prompts: trait/mood phrasing, memory-note
ranking, and system prompt assembly."""

from __future__ import annotations

from brain.personality.prompts import (
    build_system_prompt,
    describe_mood,
    describe_traits,
    rank_memory_notes,
)
from brain.personality.state import MemoryNote


def test_neutral_traits_and_mood_produce_no_lines():
    assert describe_traits({"openness": 50.0, "sarcasm": 45.0}) == []
    assert describe_mood({"energy": 65.0}) == []  # inside the 30-70 neutral band


def test_extreme_traits_map_to_directives():
    lines = describe_traits({"sarcasm": 90.0, "patience": 10.0})
    assert "dry and teasing, quick with a sarcastic aside" in lines
    assert "gets curt if things drag on" in lines


def test_honesty_humility_phrase_pair_present():
    """Every trait, including this one, must have a real phrase pair, not
    silently dropped."""
    assert describe_traits({"honesty_humility": 85.0}) == [
        "scrupulously honest and modest, even when a comfortable lie would be easier"
    ]
    assert describe_traits({"honesty_humility": 15.0}) == [
        "not above a little flattery or a convenient half-truth"
    ]


def test_unknown_trait_name_silently_ignored():
    assert describe_traits({"made_up_trait": 95.0}) == []


# --- rank_memory_notes (recency-ordered, boosted by keyword overlap) -------


def test_rank_memory_notes_boosts_keyword_overlap_above_pure_recency():
    # Recency order going in: id=3 (newest) first, id=1 (oldest) last.
    notes = [
        MemoryNote(id=3, ts=300.0, note="talked about the weather"),
        MemoryNote(id=2, ts=200.0, note="mentioned liking rust programming"),
        MemoryNote(id=1, ts=100.0, note="asked about dark mode settings"),
    ]
    ranked = rank_memory_notes(notes, "can you help me debug this rust borrow checker error?")
    assert ranked[0].id == 2  # "rust" overlap wins despite not being the most recent


def test_rank_memory_notes_stable_sort_preserves_recency_when_no_overlap():
    notes = [
        MemoryNote(id=2, ts=200.0, note="likes dark mode"),
        MemoryNote(id=1, ts=100.0, note="hates being interrupted"),
    ]
    ranked = rank_memory_notes(notes, "what's the capital of France?")
    assert [n.id for n in ranked] == [2, 1]  # unchanged -- neither matches, recency order kept


def test_rank_memory_notes_case_insensitive():
    notes = [MemoryNote(id=1, ts=100.0, note="loves RUST and Python")]
    ranked = rank_memory_notes(notes, "tell me about rust")
    assert ranked[0].id == 1


# --- build_system_prompt ------------------------------------------------------


def test_build_system_prompt_minimal():
    prompt = build_system_prompt(agent_name="Deskmate", traits={}, mood={}, memory_notes=[])
    assert "You are Deskmate" in prompt
    assert "personality" not in prompt.lower().split("\n")[0]


def test_build_system_prompt_includes_look_summary_when_given():
    prompt = build_system_prompt(
        agent_name="Deskmate",
        traits={},
        mood={},
        memory_notes=[],
        look_summary="[Appearance: Humanoid Female, wearing Wizard Robe, palette blue/amber]",
    )
    assert "[Appearance: Humanoid Female, wearing Wizard Robe, palette blue/amber]" in prompt


def test_build_system_prompt_omits_look_summary_when_none():
    prompt = build_system_prompt(agent_name="Deskmate", traits={}, mood={}, memory_notes=[])
    assert "Appearance" not in prompt


def test_build_system_prompt_includes_memory_notes():
    prompt = build_system_prompt(
        agent_name="Deskmate", traits={}, mood={}, memory_notes=["likes dark mode", "uses Rust"]
    )
    assert "likes dark mode" in prompt
    assert "uses Rust" in prompt
