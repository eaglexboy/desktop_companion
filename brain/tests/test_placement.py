"""Tests for brain.presence.placement -- pure geometry, no I/O. Covers the
corner-sequence/maximized-migration/fullscreen-hide algorithm plus
display_affinity behavior."""

from __future__ import annotations

from brain.presence.placement import Rect, ScreenInfo, WindowState, choose_position

AVATAR = (200, 200)
PRIMARY = ScreenInfo("primary", Rect(0, 0, 1920, 1080))
SECONDARY = ScreenInfo("secondary", Rect(1920, 0, 1920, 1080))


def test_no_window_defaults_to_bottom_right_of_primary():
    p = choose_position(None, [PRIMARY], AVATAR)
    assert p.screen_name == "primary"
    assert p.x == 1920 - 200 - 24
    assert p.y == 1080 - 200 - 24
    assert not p.hidden


def test_avoids_small_window_bottom_right():
    # a small window in the top-left corner shouldn't push the avatar off
    # its preferred bottom-right spot
    win = WindowState(rect=Rect(0, 0, 400, 300), screen_name="primary", fullscreen=False, desktop=1)
    p = choose_position(win, [PRIMARY], AVATAR)
    assert p.screen_name == "primary"
    assert p.x == 1920 - 200 - 24 and p.y == 1080 - 200 - 24


def test_relocates_when_window_covers_preferred_corner():
    # a window covering the whole bottom half forces the avatar to a top corner
    win = WindowState(rect=Rect(0, 400, 1920, 680), screen_name="primary", fullscreen=False, desktop=1)
    p = choose_position(win, [PRIMARY], AVATAR)
    assert p.y == 24  # pushed to a top corner
    assert not p.hidden


def test_fullscreen_moves_to_other_monitor():
    win = WindowState(rect=Rect(0, 0, 1920, 1080), screen_name="primary", fullscreen=True, desktop=1)
    p = choose_position(win, [PRIMARY, SECONDARY], AVATAR)
    assert p.screen_name == "secondary"
    assert not p.hidden


def test_fullscreen_single_monitor_marks_hidden():
    win = WindowState(rect=Rect(0, 0, 1920, 1080), screen_name="primary", fullscreen=True, desktop=1)
    p = choose_position(win, [PRIMARY], AVATAR)
    assert p.screen_name == "primary"
    assert p.hidden is True


def test_maximized_window_falls_back_to_other_monitor():
    # covers all four corners on the primary screen -> should hop to secondary
    win = WindowState(rect=Rect(0, 0, 1920, 1080), screen_name="primary", fullscreen=False, desktop=1)
    p = choose_position(win, [PRIMARY, SECONDARY], AVATAR)
    assert p.screen_name == "secondary"
    assert not p.hidden


# --- display_affinity (net-new vs. v1) ---------------------------------------


def test_affinity_primary_migrates_to_primary_when_window_is_on_secondary():
    win = WindowState(rect=Rect(1920, 0, 1920, 1080), screen_name="secondary", fullscreen=False, desktop=1)
    p = choose_position(win, [PRIMARY, SECONDARY], AVATAR, display_affinity="primary")
    assert p.screen_name == "primary"


def test_affinity_secondary_migrates_to_secondary_when_window_is_on_primary():
    win = WindowState(rect=Rect(0, 0, 1920, 1080), screen_name="primary", fullscreen=False, desktop=1)
    p = choose_position(win, [PRIMARY, SECONDARY], AVATAR, display_affinity="secondary")
    assert p.screen_name == "secondary"


def test_affinity_falls_back_to_other_screen_when_preferred_is_the_one_escaping():
    # affinity says "secondary", but the window being escaped IS the
    # secondary screen -- must fall back to primary rather than migrate
    # nowhere (or back onto the colliding screen).
    win = WindowState(rect=Rect(1920, 0, 1920, 1080), screen_name="secondary", fullscreen=False, desktop=1)
    p = choose_position(win, [PRIMARY, SECONDARY], AVATAR, display_affinity="secondary")
    assert p.screen_name == "primary"


def test_affinity_auto_behaves_like_v1_first_other_screen():
    win = WindowState(rect=Rect(0, 0, 1920, 1080), screen_name="primary", fullscreen=False, desktop=1)
    p = choose_position(win, [PRIMARY, SECONDARY], AVATAR, display_affinity="auto")
    assert p.screen_name == "secondary"


def test_affinity_secondary_with_no_second_screen_falls_back_to_primary():
    win = WindowState(rect=Rect(0, 0, 1920, 1080), screen_name="primary", fullscreen=True, desktop=1)
    p = choose_position(win, [PRIMARY], AVATAR, display_affinity="secondary")
    assert p.screen_name == "primary"
    assert p.hidden is True


def test_is_own_window_matches_only_the_avatars_own_rect():
    from brain.presence.placement import Placement, is_own_window

    placement = Placement(4776, 1096, "DP-3")
    own = WindowState(Rect(4776, 1096, 320, 320), "DP-3", False, 1)
    assert is_own_window(own, placement, (320, 320))
    assert is_own_window(WindowState(Rect(4778, 1095, 320, 321), "DP-3", False, 1), placement, (320, 320))  # tolerance
    # a user window that merely overlaps, or is the same size elsewhere, is not "us"
    assert not is_own_window(WindowState(Rect(0, 0, 2560, 1396), "DP-2", False, 1), placement, (320, 320))
    assert not is_own_window(WindowState(Rect(2584, 1096, 320, 320), "DP-3", False, 1), placement, (320, 320))
    assert not is_own_window(WindowState(Rect(4776, 1096, 800, 600), "DP-3", False, 1), placement, (320, 320))
