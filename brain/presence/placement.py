"""Pure geometry: given the active window (fed in by desktop_bridge.py from
the KWin script) and the screen layout, decide where the overlay should
stand. Kept dependency-free and pure so it's trivially unit-testable -- no
D-Bus, no IPC, just rectangles in, a Placement out.

Implements a corner-sequence/maximized-migration/fullscreen-hide placement
algorithm. `choose_position()` takes a `display_affinity` parameter
(brain/config.py's `PresenceConfig.display_affinity`) so a migration away
from the window's screen honors "primary"/"secondary"/"auto" rather than
always picking whichever other screen comes first in the list.

Convention (no screen in the wire protocol is ever flagged "primary" --
see adapters/kwin/contents/code/main.js's screensData, just name/x/y/w/h):
screens[0], in whatever order KWin's `workspace.screens` reports them, is
treated as the primary monitor -- the only ordering signal actually
available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DEFAULT_MARGIN = 24

DisplayAffinity = Literal["primary", "secondary", "auto"]


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def overlaps(self, other: Rect) -> bool:
        return self.x < other.right and self.right > other.x and self.y < other.bottom and self.bottom > other.y


@dataclass(frozen=True)
class ScreenInfo:
    name: str
    rect: Rect


@dataclass(frozen=True)
class WindowState:
    rect: Rect
    screen_name: str | None
    fullscreen: bool
    desktop: int


@dataclass(frozen=True)
class Placement:
    x: int
    y: int
    screen_name: str
    hidden: bool = False  # true only when overlap is truly unavoidable (single fullscreen screen)


def is_own_window(window: WindowState, placement: Placement, avatar_size: tuple[int, int], tolerance: int = 4) -> bool:
    """True if `window` is the avatar's own overlay surface rather than a user
    window: it sits exactly where we last placed the avatar and has exactly
    the avatar's size. Matching on geometry instead of a window class/name is
    deliberate -- the sensing adapter's own name-based self-check can fail
    (a layer-shell surface's reported class is not guaranteed), and if the
    overlay's own window ever reaches placement it is treated as an obstacle:
    the avatar flees its own corner, its new window is reported, it flees
    back, and it flaps between corners forever."""
    return (
        abs(window.rect.x - placement.x) <= tolerance
        and abs(window.rect.y - placement.y) <= tolerance
        and abs(window.rect.w - avatar_size[0]) <= tolerance
        and abs(window.rect.h - avatar_size[1]) <= tolerance
    )


def _corner_candidates(screen: Rect, avatar_w: int, avatar_h: int, margin: int) -> list[tuple[int, int]]:
    return [
        (screen.right - avatar_w - margin, screen.bottom - avatar_h - margin),  # bottom-right
        (screen.x + margin, screen.bottom - avatar_h - margin),  # bottom-left
        (screen.right - avatar_w - margin, screen.y + margin),  # top-right
        (screen.x + margin, screen.y + margin),  # top-left
    ]


def _primary_screen(screens: list[ScreenInfo]) -> ScreenInfo:
    if not screens:
        raise ValueError("choose_position needs at least one screen")
    return screens[0]


def _migration_target(screens: list[ScreenInfo], exclude_name: str | None, affinity: DisplayAffinity) -> ScreenInfo | None:
    """Picks the screen to migrate to when the window's own screen is
    unusable (maximized/colliding or fullscreen), honoring
    `display_affinity`. Falls back to any other screen if the affinity's
    preferred target turns out to BE the screen we're trying to escape."""
    others = [s for s in screens if s.name != exclude_name]
    if not others:
        return None
    if affinity == "auto":
        return others[0]

    primary = screens[0]
    secondary = screens[1] if len(screens) > 1 else None
    preferred = primary if affinity == "primary" else (secondary or primary)
    return preferred if preferred.name != exclude_name else others[0]


def choose_position(
    window: WindowState | None,
    screens: list[ScreenInfo],
    avatar_size: tuple[int, int],
    margin: int = DEFAULT_MARGIN,
    display_affinity: DisplayAffinity = "secondary",
) -> Placement:
    avatar_w, avatar_h = avatar_size
    if not window:
        screen = _primary_screen(screens)
        x, y = _corner_candidates(screen.rect, avatar_w, avatar_h, margin)[0]
        return Placement(x, y, screen.name)

    screen_by_name = {s.name: s for s in screens}
    window_screen = screen_by_name.get(window.screen_name) if window.screen_name else None

    if window.fullscreen:
        target = _migration_target(screens, window.screen_name, display_affinity)
        if target is not None:
            x, y = _corner_candidates(target.rect, avatar_w, avatar_h, margin)[0]
            return Placement(x, y, target.name)
        # only one screen and it's fullscreen: nothing left to do but tuck
        # into a corner and let the overlay itself fade/peek there.
        fallback = window_screen or _primary_screen(screens)
        x, y = _corner_candidates(fallback.rect, avatar_w, avatar_h, margin)[0]
        return Placement(x, y, fallback.name, hidden=True)

    target = window_screen or _primary_screen(screens)

    def avatar_rect_at(x: int, y: int) -> Rect:
        return Rect(x, y, avatar_w, avatar_h)

    for x, y in _corner_candidates(target.rect, avatar_w, avatar_h, margin):
        if not avatar_rect_at(x, y).overlaps(window.rect):
            return Placement(x, y, target.name)

    # every corner of this screen collides with the window (it's maximized
    # or nearly so): prefer another screen if we have one.
    alt = _migration_target(screens, window.screen_name, display_affinity)
    if alt is not None:
        x, y = _corner_candidates(alt.rect, avatar_w, avatar_h, margin)[0]
        return Placement(x, y, alt.name)

    # no alternative: least-bad corner (bottom-right) even though it overlaps.
    x, y = _corner_candidates(target.rect, avatar_w, avatar_h, margin)[0]
    return Placement(x, y, target.name)
