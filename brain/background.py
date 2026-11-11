"""
The daemon's background loops: reflection, mood decay and broadcast, voice cache
clean-up.
"""

from __future__ import annotations

import asyncio
import logging
import time

from brain.app_state import AppState
from brain.appearance.blender_gen import is_build_running
from brain.personality.mood_label import mood_label
from brain.personality.reflect import (
    maybe_trigger_autonomous_redesign,
    run_reflection,
)

logger = logging.getLogger("deskmate.main")

_GC_INTERVAL_S = 300.0
_GC_TTL_S = 600.0


# --- background loops -----------------------------------------------------------


async def _reflection_loop(state: AppState) -> None:
    """One combined cycle: autonomous-redesign eligibility is evaluated in
    the same reflect.py pass as personality reflection itself, not on a
    separate timer."""
    while True:
        await asyncio.sleep(state.settings.personality.reflection_interval_s)
        try:
            await run_reflection(state.store, state.local_pool)
        except Exception:
            logger.exception("reflection loop iteration failed")
        try:
            await maybe_trigger_autonomous_redesign(
                state.store,
                is_build_running=is_build_running,
                is_user_interacting=lambda: state.interaction_active,
                redesign_cooldown_hours_min=state.settings.appearance.redesign_cooldown_hours_min,
                redesign_cooldown_hours_max=state.settings.appearance.redesign_cooldown_hours_max,
                autonomous_redesign_probability=state.settings.appearance.autonomous_redesign_probability,
                on_eligible=state.evolution.trigger_b_on_eligible,
            )
        except Exception:
            logger.exception("Trigger-B eligibility check failed")


async def _mood_decay_loop(state: AppState) -> None:
    while True:
        await asyncio.sleep(state.settings.personality.mood_decay_interval_s)
        try:
            state.store.decay_mood(state.settings.personality.mood_half_life_s)
        except Exception:
            logger.exception("mood decay loop iteration failed")


async def _mood_broadcast_loop(state: AppState) -> None:
    """Tells the overlay how the companion feels, as a label (`mood_label`) and the dimensions, so
    that it plays the idles that go with it (a droop when low, folded arms when bored...).
    """
    while True:
        try:
            mood = state.store.get_mood()
            await state.hub.broadcast(
                "mood_update", {"mood": mood_label(mood), **mood}
            )
        except Exception:
            logger.exception("mood broadcast loop iteration failed")
        await asyncio.sleep(_MOOD_BROADCAST_INTERVAL_S)


_MOOD_BROADCAST_INTERVAL_S = 30.0


async def _gc_loop(state: AppState) -> None:
    """Sweeps ephemeral synthesized-speech WAVs older than _GC_TTL_S --
    these are write-once playback artifacts (GET /audio/{utterance_id}.wav),
    never read again once the overlay has played them."""
    while True:
        await asyncio.sleep(_GC_INTERVAL_S)
        try:
            cutoff = time.time() - _GC_TTL_S
            for wav_path in state.settings.voice_cache_dir.glob("*.wav"):
                try:
                    if wav_path.stat().st_mtime < cutoff:
                        wav_path.unlink()
                except OSError:
                    pass  # already gone, or a permissions race -- not worth logging every tick
        except Exception:
            logger.exception("voice cache GC iteration failed")
