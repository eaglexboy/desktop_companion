"""Deterministic trait/mood -> delivery-gesture mapping, consumed when
assembling a `speech` envelope's `delivery` field. Kept as its own small
module since it operates purely on personality data (traits/mood dicts),
not on any TTS backend -- it has nothing to do with brain/voice/tts.py's
synthesis plumbing.
"""

from __future__ import annotations

from typing import Literal

DeliveryStyle = Literal["deadpan", "animated", "subdued", "explanatory"]

_SARCASM_DEADPAN_THRESHOLD = 70.0
_ENERGY_ANIMATED_THRESHOLD = 70.0
_ENERGY_SUBDUED_THRESHOLD = 30.0


def pick_delivery(traits: dict[str, float], mood: dict[str, float], is_long_or_code: bool) -> DeliveryStyle:
    """The rule table (`deadpan` if sarcasm >= 70, `animated` if energy >=
    70, `subdued` if energy <= 30, `explanatory` for code/lengthy
    explanations) names no priority order for when more than one condition
    holds at once. Resolved here, as a judgment call, in this order:

    1. `is_long_or_code` first -- a user asking for code or a long
       explanation wants clear, explanatory delivery regardless of the
       agent's current mood/trait state.
    2. The two trait/mood rules next: sarcasm-driven `deadpan`, then
       energy-driven `animated`/`subdued`.
    3. `animated` as the fallback when nothing matches -- a companion with
       no strong signal either way defaults to its normal lively
       presentation rather than a flatter style.
    """
    if is_long_or_code:
        return "explanatory"
    if traits.get("sarcasm", 50.0) >= _SARCASM_DEADPAN_THRESHOLD:
        return "deadpan"
    energy = mood.get("energy", 50.0)
    if energy >= _ENERGY_ANIMATED_THRESHOLD:
        return "animated"
    if energy <= _ENERGY_SUBDUED_THRESHOLD:
        return "subdued"
    return "animated"
