"""The one-word mood the overlay acts out (which idles it plays), from the mood dimensions."""

from __future__ import annotations

from collections.abc import Mapping


def mood_label(mood: Mapping[str, float]) -> str:
    """`fidget` (stressed), `sad` (low valence), `bored` (low energy), `content` (high valence),
    `curious` (high energy) or `neutral`, in that order of precedence. 50 is the resting value of
    each dimension, so a companion that has not been moved by anything is neutral."""
    valence = mood.get("valence", 50.0)
    energy = mood.get("energy", 50.0)
    stress = mood.get("stress", 50.0)
    if stress >= 70:
        return "fidget"
    if valence <= 35:
        return "sad"
    if energy <= 35:
        return "bored"
    if valence >= 65:
        return "content"
    if energy >= 65:
        return "curious"
    return "neutral"
