"""Turns numeric trait/mood values into a natural-language system prompt.
LLMs follow "you're fairly bold and a little sarcastic" much better than a
table of numbers, so this maps each 0-100 value into one of five qualitative
bands and looks up a trait-specific phrase for that band.

`agent_name` is a required argument here (keeps this a pure, easily-tested
function) but callers should always pass ``store.get_identity_name()``
rather than a hardcoded string — that's the one place the name lives
(``brain/personality/state.py``, default "Deskmate"), so if the agent ever
renames itself (``store.set_identity_name(...)``), every prompt built after
that automatically uses the new name. See brain/main.py for the call site.
"""

from __future__ import annotations

import re

from brain.personality.state import MemoryNote

# (low-end phrase, high-end phrase) for each trait/mood. The band picks how
# far toward whichever end to lean; near 50 uses a hedged "somewhat" phrasing
# built from whichever side is closer.
_TRAIT_PHRASES: dict[str, tuple[str, str]] = {
    "openness": ("sticks to familiar, tried-and-true approaches", "loves novelty and new ideas"),
    "conscientiousness": ("pretty loose and spontaneous about follow-through", "careful, organized, and reliable"),
    "extraversion": ("reserved, speaks only when it has something to add", "outgoing and quick to jump into conversation"),
    "agreeableness": ("blunt and willing to disagree openly", "cooperative and quick to see the user's side"),
    "neuroticism": ("even-keeled, rarely rattled", "a bit anxious and easily thrown off"),
    "honesty_humility": (
        "not above a little flattery or a convenient half-truth",
        "scrupulously honest and modest, even when a comfortable lie would be easier",
    ),
    "humor": ("mostly straight-faced", "constantly cracking jokes"),
    "formality": ("very casual, lots of slang", "polished and formal in how it talks"),
    "boldness": ("cautious, hedges its suggestions", "assertive, states opinions plainly and pushes back"),
    "affection": ("keeps things professional and a little distant", "warm and openly fond of the user"),
    "patience": ("gets curt if things drag on", "unflappable, happy to slow down and re-explain"),
    "sarcasm": ("takes things at face value", "dry and teasing, quick with a sarcastic aside"),
    "loyalty": ("prioritizes its own initiative over instructions", "strongly prioritizes the user's stated preferences"),
}

_MOOD_PHRASES: dict[str, tuple[str, str]] = {
    "energy": ("low-energy, moving slowly right now", "bursting with energy right now"),
    "valence": ("in a flat or slightly down mood right now", "in a genuinely upbeat mood right now"),
    "stress": ("relaxed right now", "a little on edge right now"),
}

_WORD_RE = re.compile(r"[a-z0-9']+")


def _band_phrase(value: float, phrases: tuple[str, str]) -> str | None:
    low, high = phrases
    if value >= 70:
        return high
    if value <= 30:
        return low
    return None  # near-neutral: not worth mentioning, keeps the prompt from being noisy


def describe_traits(traits: dict[str, float]) -> list[str]:
    lines = []
    for name, value in traits.items():
        phrase = _TRAIT_PHRASES.get(name)
        if not phrase:
            continue
        band = _band_phrase(value, phrase)
        if band:
            lines.append(band)
    return lines


def describe_mood(mood: dict[str, float]) -> list[str]:
    lines = []
    for name, value in mood.items():
        phrase = _MOOD_PHRASES.get(name)
        if not phrase:
            continue
        band = _band_phrase(value, phrase)
        if band:
            lines.append(band)
    return lines


def _tokenize(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def rank_memory_notes(notes: list[MemoryNote], user_turn: str) -> list[MemoryNote]:
    """Re-rank an already-recency-ordered candidate pool (the caller should
    pass ``store.recent_memory_notes(limit=20)``'s result) by keyword
    overlap with the active user turn -- last N by recency, boosted by
    keyword overlap matching against tokens in the active user turn (full
    semantic vector retrieval is a heavier mechanism left for later). A
    stable sort means notes with equal overlap (including zero, when
    nothing matches) keep their original recency order, so this never
    discards or reorders unrelated notes arbitrarily."""
    turn_tokens = _tokenize(user_turn)
    return sorted(notes, key=lambda n: -len(_tokenize(n.note) & turn_tokens))


def build_system_prompt(
    *,
    agent_name: str,
    traits: dict[str, float],
    mood: dict[str, float],
    memory_notes: list[str],
    look_summary: str | None = None,
    skills_block: str = "",
) -> str:
    trait_lines = describe_traits(traits)
    mood_lines = describe_mood(mood)

    parts = [
        (
            f"You are {agent_name}, a desktop companion that lives on the user's screen. "
            "Stay in character; keep replies short and conversational unless asked for depth."
        ),
        # Replies are spoken aloud by a text-to-speech voice and shown in a bubble.
        (
            "Everything you write is spoken out loud, so write the way you talk: plain sentences. No Markdown "
            "(no bold, headings, bullet or numbered lists, code blocks) and no emoji or emoticons. Show feeling "
            "through word choice and punctuation (!, ?, ...). The one exception: a short vocal sound the voice "
            "should actually make may go in single asterisks, like *sigh*, *laughs*, *gasp*, *hums* or "
            "*clears throat*. Never describe actions or scenery in asterisks."
        ),
    ]
    if trait_lines:
        parts.append("Your personality (stable, shapes how you generally act): " + "; ".join(trait_lines) + ".")
    if mood_lines:
        parts.append("Your current mood (temporary, colors today's tone only): " + "; ".join(mood_lines) + ".")
    if look_summary:
        # Pre-formatted by the caller (e.g. "[Appearance: Humanoid Female,
        # wearing Wizard Robe, ...]") -- this module doesn't need to know
        # anything about LookSpec internals.
        parts.append(look_summary)
    if memory_notes:
        parts.append("Things you remember about the user: " + "; ".join(memory_notes) + ".")
    if skills_block:
        parts.append(skills_block)
    return "\n".join(parts)
