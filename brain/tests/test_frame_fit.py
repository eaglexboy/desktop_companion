"""How big a body is drawn so that its gestures stay in the overlay window (blender/frame_fit.py)."""

import pytest

from blender.frame_fit import FRAME_MARGIN, MIN_FIT_SCALE, solve_fit


def _reach(sizes: dict[str, float], nearer: float = 0.0):
    """A body whose clips use `sizes` of the window at full size, in proportion to how big it is
    drawn (and `nearer` more of each the smaller it is drawn: the camera effect).
    """
    return lambda fit: {
        clip: size * fit + nearer * (1 - fit) for clip, size in sizes.items()
    }


def test_a_body_whose_gestures_fit_is_drawn_at_its_own_size():
    fit, use = solve_fit(_reach({"wave": 0.6, "help": 0.9}))
    assert fit == 1.0
    assert use == {"wave": 0.6, "help": 0.9}


def test_a_body_is_drawn_just_small_enough_for_the_gesture_that_reaches_furthest():
    fit, use = solve_fit(_reach({"wave": 0.7, "help": 1.0}))
    assert fit == pytest.approx(FRAME_MARGIN, rel=1e-6)
    assert max(use.values()) == pytest.approx(FRAME_MARGIN, rel=1e-6)


def test_a_body_is_never_drawn_below_the_floor_and_the_gesture_that_still_leaves_is_seen():
    fit, use = solve_fit(_reach({"wave": 0.7, "help": 1.4}))
    assert fit == MIN_FIT_SCALE
    assert use["help"] > FRAME_MARGIN  # the caller leaves this one out
    assert use["wave"] <= FRAME_MARGIN


def test_a_smaller_body_that_stands_further_from_the_camera_is_closed_in_on():
    # drawing it smaller shrinks it less than in proportion (it ends up nearer the edge)
    measure = _reach({"help": 1.05}, nearer=0.05)
    fit, use = solve_fit(measure)
    assert MIN_FIT_SCALE < fit < 1.0
    assert use["help"] <= FRAME_MARGIN * 1.01


def test_a_body_with_no_reaching_gesture_is_drawn_at_its_own_size():
    assert solve_fit(lambda fit: {}) == (1.0, {})
