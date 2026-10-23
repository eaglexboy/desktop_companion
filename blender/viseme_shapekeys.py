"""Canonical viseme set, shared between the Blender generator (which must
create a shape key per code on any face-bearing model) and the brain's
lip-sync pipeline (which drives those same shape keys' weights at runtime).

We reuse Rhubarb Lip Sync's own 9-shape mouth-chart directly — it's the
free, standard tool doing the audio analysis, so there's no value in
inventing a separate viseme taxonomy and mapping between the two. Rhubarb's
`value` field in its JSON output is already one of these codes.

Reference: https://github.com/DanielSWolf/rhubarb-lip-sync — the mouth
chart in its docs shows what each shape looks like (X is the closed/idle
mouth every model needs regardless of whether it talks).
"""

from __future__ import annotations

VISEME_CODES: tuple[str, ...] = ("A", "B", "C", "D", "E", "F", "G", "H", "X")

SHAPE_KEY_PREFIX = "viseme_"


def shape_key_name(code: str) -> str:
    if code not in VISEME_CODES:
        raise ValueError(f"Unknown viseme code {code!r}, expected one of {VISEME_CODES}")
    return f"{SHAPE_KEY_PREFIX}{code}"


ALL_SHAPE_KEY_NAMES: tuple[str, ...] = tuple(shape_key_name(c) for c in VISEME_CODES)
