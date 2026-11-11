"""
Fixed tables of the generator: the budgets' companions, the clip lists, the
viseme mouth matrix, the talk-pulse numbers.
"""

from __future__ import annotations

CORE_IDLE_CLIPS: tuple[tuple[str, bool, float], ...] = (
    ("idle_lookaround", True, 4.0),
    ("idle_stretch", True, 3.0),
    ("idle_peek", False, 2.5),
    ("idle_blink", True, 3.5),
)


STATE_LOOP_CLIPS: tuple[tuple[str, bool, float], ...] = (
    ("move", True, 1.0),
    ("think", True, 3.0),
)


MOOD_IDLE_CLIPS: tuple[tuple[str, bool, float], ...] = (
    ("idle_bored", True, 5.0),
    ("idle_curious", True, 4.0),
    ("idle_sleepy", True, 6.0),
    ("idle_fidget", True, 3.0),
    ("idle_focused", True, 4.0),
)


DELIVERY_CLIPS: tuple[tuple[str, bool, float], ...] = (
    ("talk_subdued", True, 2.0),
    ("talk_animated", True, 2.0),
    ("talk_explanatory", True, 2.5),
    ("talk_deadpan", True, 2.0),
)


AUXILIARY_CLIPS: tuple[tuple[str, bool, float], ...] = (
    ("greet", False, 1.8),
    ("celebrate", False, 2.2),
    ("shrug", False, 1.5),
    ("agree", False, 1.2),
    ("disagree", False, 1.2),
)


# (Open, Width, Round, Teeth) per viseme. Teeth is 0/1 and drives a small
# secondary teeth-strip mesh's visibility rather than the mouth plane itself.
VISEME_MOUTH_MATRIX: dict[str, tuple[float, float, float, int]] = {
    "X": (0.0, 1.0, 0.0, 0),
    "A": (0.0, 0.9, 0.0, 0),
    "B": (0.2, 1.1, 0.0, 1),
    "C": (0.5, 1.0, 0.1, 0),
    "D": (1.0, 1.0, 0.2, 0),
    "E": (0.7, 0.7, 0.8, 0),
    "F": (0.3, 0.5, 1.0, 0),
    "G": (0.2, 1.0, 0.0, 1),
    "H": (0.5, 0.9, 0.0, 0),
}
