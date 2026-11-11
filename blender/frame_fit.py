"""How big the overlay draws a body so that its gestures stay in the overlay window: the search,
without Blender (gen_models measures how much of the window a clip uses; this decides what to do
about it)."""

from collections.abc import Callable

FRAME_MARGIN = 0.97  # how much of the window a clip may use
# How small the overlay may draw a body (a fraction of its own size); a gesture that still leaves
# the window at this size is not used.
MIN_FIT_SCALE = 0.85


def solve_fit(
    measure: Callable[[float], dict[str, float]],
    *,
    margin: float = FRAME_MARGIN,
    floor: float = MIN_FIT_SCALE,
    rounds: int = 4,
) -> tuple[float, dict[str, float]]:
    """(fit, use): `measure(fit)` is how much of the window each clip uses (1.0 is the edge) when
    the body is drawn at `fit` times its size. The fit is 1.0 when every clip is inside the margin,
    otherwise just small enough for the clip that reaches furthest, never below `floor`. Drawing
    smaller also moves the body away from the camera, so the first guess (in proportion) is closed
    in on over a few rounds. `use` is the measurement at the fit that is returned.
    """
    fit = 1.0
    use = measure(fit)
    for _ in range(rounds):
        worst = max(use.values(), default=0.0)
        if worst <= margin or fit <= floor:
            break
        fit = max(floor, fit * margin / worst)
        use = measure(fit)
    return fit, use
