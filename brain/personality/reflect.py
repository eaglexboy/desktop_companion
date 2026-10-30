"""Periodic reflection: looks at interactions since the last reflection and
asks the local model for small trait/mood nudges plus any memory notes worth
keeping. This is *the* mechanism behind "personality that develops over
time" — deltas are deliberately capped small so it drifts, it doesn't swing.

Also evaluates autonomous-redesign eligibility on the same cadence. That
function only *decides whether* to fire -- it doesn't perform any redesign
itself; the caller wires the real ``on_eligible`` callable to
``compile_and_publish()``.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from brain.config import MOOD_NAMES, TRAIT_NAMES
from brain.events import emit, llm_purpose
from brain.llm_pool import LLMPool
from brain.personality.state import PersonalityStore

logger = logging.getLogger("deskmate.reflect")


@dataclass(frozen=True)
class ReflectionResult:
    """`bool(result)` still works (truthy iff `changed`), so this is a
    backward-compatible return type for anything that only ever cared
    whether a pass did something -- but callers like `POST /reflect` need
    the actual counts/deltas a bare bool can't carry."""

    changed: bool
    trait_deltas: dict[str, float] = field(default_factory=dict)
    mood_deltas: dict[str, float] = field(default_factory=dict)
    notes_created: int = 0

    def __bool__(self) -> bool:
        return self.changed


_NO_OP_RESULT = ReflectionResult(changed=False)

# Hard caps applied regardless of what the model proposes — a bad reflection
# call should never be able to wreck the personality in one shot.
_MAX_TRAIT_DELTA = 5.0
_MAX_MOOD_DELTA = 20.0
_MAX_MEMORY_NOTES_PER_PASS = 5

_REFLECTION_PROMPT_TEMPLATE = """\
You are the reflection process behind a desktop companion agent's evolving \
personality. Below is a transcript of its most recent interactions with the \
user. Suggest small adjustments — this happens repeatedly over time, so \
personality should drift gradually, not swing.

Known trait names: {trait_names}
Known mood names: {mood_names}

Respond with ONLY compact JSON of this shape, omitting any trait/mood you
don't want to adjust:
{{
  "trait_deltas": {{"<trait_name>": <float between -5 and 5>, ...}},
  "mood_deltas": {{"<mood_name>": <float between -20 and 20>, ...}},
  "memory_notes": ["<short factual note about the user or the interaction>", ...]
}}

Transcript:
{transcript}
"""


def _format_transcript(rows) -> str:
    lines = []
    for row in rows:
        tag = row["role"]
        if row["role"] == "assistant":
            tag += " (cloud)" if row["escalated"] else " (local)"
        lines.append(f"{tag}: {row['text']}")
    return "\n".join(lines)


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


async def run_reflection(store: PersonalityStore, local_pool: LLMPool) -> ReflectionResult:
    """Run one reflection pass over pending interactions."""
    pending = store.pending_interactions()
    if not pending:
        return _NO_OP_RESULT
    if local_pool.is_empty:
        logger.info("no local LLM server configured, skipping reflection")
        return _NO_OP_RESULT

    prompt = _REFLECTION_PROMPT_TEMPLATE.format(
        trait_names=", ".join(TRAIT_NAMES),
        mood_names=", ".join(MOOD_NAMES),
        transcript=_format_transcript(pending),
    )

    try:
        with llm_purpose("reflect"):
            _, raw = await local_pool.chat([{"role": "user", "content": prompt}], max_tokens=512)
        verdict = json.loads(raw.strip())
    except Exception as exc:  # noqa: BLE001 - a bad reflection pass shouldn't crash the daemon
        logger.warning("reflection call failed: %s", exc)
        return _NO_OP_RESULT

    trait_deltas = {
        name: _clamp(float(delta), _MAX_TRAIT_DELTA)
        for name, delta in verdict.get("trait_deltas", {}).items()
        if name in TRAIT_NAMES
    }
    mood_deltas = {
        name: _clamp(float(delta), _MAX_MOOD_DELTA)
        for name, delta in verdict.get("mood_deltas", {}).items()
        if name in MOOD_NAMES
    }
    memory_notes = [str(n) for n in verdict.get("memory_notes", [])][:_MAX_MEMORY_NOTES_PER_PASS]

    if trait_deltas:
        store.adjust_traits(trait_deltas, cause="reflection")
    if mood_deltas:
        store.nudge_mood(mood_deltas, cause="reflection")
    emit(
        "reflection.result",
        {"trait_deltas": trait_deltas, "mood_deltas": mood_deltas, "memory_notes": memory_notes,
         "interactions_reviewed": len(pending)},
    )
    for note in memory_notes:
        store.add_memory_note(note)
    store.prune_memory_notes()
    store.mark_reflected([row["id"] for row in pending])

    logger.info(
        "reflection: %d interactions, trait_deltas=%s mood_deltas=%s notes=%d",
        len(pending),
        trait_deltas,
        mood_deltas,
        len(memory_notes),
    )
    return ReflectionResult(
        changed=True, trait_deltas=trait_deltas, mood_deltas=mood_deltas, notes_created=len(memory_notes)
    )


async def maybe_trigger_autonomous_redesign(
    store: PersonalityStore,
    *,
    is_build_running: Callable[[], bool],
    is_user_interacting: Callable[[], bool],
    redesign_cooldown_hours_min: float,
    redesign_cooldown_hours_max: float,
    autonomous_redesign_probability: float,
    on_eligible: Callable[[], Awaitable[None]],
) -> bool:
    """Evaluate autonomous-redesign eligibility: no look already staged, cooldown elapsed (uniformly
    sampled 12-24h, fresh each call rather than persisted -- statistically
    equivalent to sampling once per redesign and needs no extra
    agent_state key), no compile running, user not actively interacting,
    then a probability roll. Calls ``on_eligible()`` and returns True only
    if every gate passes; never performs a redesign itself. Trait/mood
    *biasing* of what changes belongs in the delta-synthesis prompt, not
    here -- this function only gates *whether* a cycle fires.
    """
    def _verdict(outcome: str, **detail: float) -> None:
        # Every gate is silent by nature; without this a "decided not to"
        # can't be told apart from "never checked".
        logger.info("autonomous redesign check: %s %s", outcome, detail or "")
        emit("autonomous.check", {"outcome": outcome, **detail})

    if is_build_running():
        _verdict("build_running")
        return False
    if is_user_interacting():
        _verdict("user_interacting")
        return False

    # A staged look is still waiting to be revealed; building another would
    # just overwrite it (and its cooldown only starts at reveal time).
    if store.get_agent_state("staged_version") is not None:
        _verdict("staged_pending")
        return False

    last_redesign_raw = store.get_agent_state("last_redesign_timestamp")
    if last_redesign_raw is not None:
        elapsed_hours = (time.time() - float(last_redesign_raw)) / 3600.0
        threshold_hours = random.uniform(redesign_cooldown_hours_min, redesign_cooldown_hours_max)
        if elapsed_hours < threshold_hours:
            _verdict("cooldown", elapsed_h=round(elapsed_hours, 2), threshold_h=round(threshold_hours, 2))
            return False

    roll = random.random()
    if roll >= autonomous_redesign_probability:
        _verdict("roll_failed", roll=round(roll, 3), needed_below=autonomous_redesign_probability)
        return False

    _verdict("triggered", roll=round(roll, 3))
    await on_eligible()
    return True
