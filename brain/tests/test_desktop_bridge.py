"""Tests for brain.presence.desktop_bridge: the pure PresenceStateMachine's
full transition table (no D-Bus needed), the _parse_payload helper, and
real integration tests against a live session bus (the KWin-telemetry
server) and, where feasible, a faked ScreenSaver-like signal source
standing in for the real org.freedesktop.ScreenSaver service, since
claiming that well-known bus name ourselves isn't possible without actually
being kwin_wayland. Skipped automatically where no session bus is available.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import pytest

from brain.presence.desktop_bridge import (
    DesktopPresenceBridge,
    PresenceSignals,
    PresenceState,
    PresenceStateMachine,
    _parse_payload,
)
from brain.presence.placement import Rect, ScreenInfo, WindowState

HAS_SESSION_BUS = bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
HAS_LOGIN1_SESSION = bool(os.environ.get("XDG_SESSION_ID")) and shutil.which("busctl") is not None


# --- PresenceStateMachine: full transition table (pure, no D-Bus) -----------


def _signals(*, screensaver=False, locked=False, idle_hint=False, seconds=0.0) -> PresenceSignals:
    return PresenceSignals(
        screensaver_active=screensaver, locked_hint=locked, idle_hint=idle_hint, seconds_since_activity=seconds
    )


@pytest.mark.asyncio
async def test_starts_active():
    machine = PresenceStateMachine(idle_after_s=600)
    assert machine.state == PresenceState.ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signals,expected",
    [
        (_signals(), PresenceState.ACTIVE),
        (_signals(seconds=599), PresenceState.ACTIVE),
        (_signals(seconds=601), PresenceState.IDLE),
        (_signals(screensaver=True), PresenceState.AWAY),
        (_signals(locked=True), PresenceState.AWAY),
        (_signals(idle_hint=True), PresenceState.AWAY),
        # Away takes priority over a simultaneous idle-timeout condition.
        (_signals(screensaver=True, seconds=9999), PresenceState.AWAY),
        (_signals(locked=True, screensaver=True), PresenceState.AWAY),
    ],
)
async def test_full_transition_table(signals, expected):
    machine = PresenceStateMachine(idle_after_s=600)
    assert await machine.update(signals) == expected


@pytest.mark.asyncio
async def test_on_transition_fires_only_on_actual_state_change():
    transitions = []

    async def on_transition(old, new):
        transitions.append((old, new))

    machine = PresenceStateMachine(idle_after_s=600, on_transition=on_transition)
    await machine.update(_signals())  # still Active, no transition
    assert transitions == []

    await machine.update(_signals(locked=True))  # Active -> Away
    assert transitions == [(PresenceState.ACTIVE, PresenceState.AWAY)]

    await machine.update(_signals(locked=True))  # still Away, no transition
    assert transitions == [(PresenceState.ACTIVE, PresenceState.AWAY)]

    await machine.update(_signals())  # Away -> Active
    assert transitions == [(PresenceState.ACTIVE, PresenceState.AWAY), (PresenceState.AWAY, PresenceState.ACTIVE)]


@pytest.mark.asyncio
async def test_idle_to_away_and_back_to_idle_not_active():
    """Going Idle -> Away -> (signal clears) should land back on whatever
    the idle-timeout condition alone implies (Idle), not jump straight to
    Active just because the Away-specific signal cleared."""
    machine = PresenceStateMachine(idle_after_s=600)
    await machine.update(_signals(seconds=601))
    assert machine.state == PresenceState.IDLE
    await machine.update(_signals(locked=True, seconds=601))
    assert machine.state == PresenceState.AWAY
    await machine.update(_signals(seconds=601))
    assert machine.state == PresenceState.IDLE


# --- _parse_payload -----------------------------------------------------


def test_parse_payload_with_window():
    raw = json.dumps(
        {
            "window": {
                "x": 10,
                "y": 20,
                "width": 300,
                "height": 400,
                "screen": "DP-1",
                "fullscreen": False,
                "desktop": 2,
            },
            "screens": [{"name": "DP-1", "x": 0, "y": 0, "width": 1920, "height": 1080}],
        }
    )
    window, screens = _parse_payload(raw)
    assert window == WindowState(Rect(10, 20, 300, 400), "DP-1", False, 2)
    assert screens == [ScreenInfo("DP-1", Rect(0, 0, 1920, 1080))]


def test_parse_payload_no_window():
    raw = json.dumps({"window": None, "screens": []})
    window, screens = _parse_payload(raw)
    assert window is None
    assert screens == []


# --- live D-Bus integration: KWin telemetry server ---------------------------


@pytest.mark.skipif(not HAS_SESSION_BUS, reason="needs a live D-Bus session bus")
@pytest.mark.asyncio
async def test_service_receives_real_dbus_call_and_updates_presence():
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.asyncio import open_dbus_router

    service_name = "org.deskmate.PresenceTest"
    object_path = "/org/deskmate/PresenceTest"
    received: list[tuple] = []

    async def on_update(window, screens):
        received.append((window, screens))

    bridge = DesktopPresenceBridge(service_name, object_path, idle_after_s=600, on_update=on_update)
    run_task = asyncio.create_task(bridge._run_kwin_server())
    await asyncio.sleep(0.3)  # let it claim the bus name and start listening

    try:
        addr = DBusAddress(object_path, bus_name=service_name, interface=service_name)
        payload = json.dumps(
            {
                "window": {
                    "x": 0,
                    "y": 0,
                    "width": 800,
                    "height": 600,
                    "screen": "DP-1",
                    "fullscreen": False,
                    "desktop": 1,
                },
                "screens": [{"name": "DP-1", "x": 0, "y": 0, "width": 1920, "height": 1080}],
            }
        )
        call = new_method_call(addr, "UpdateState", "s", (payload,))
        async with open_dbus_router(bus="SESSION") as client_router:
            reply = await asyncio.wait_for(client_router.send_and_get_reply(call), timeout=5)
            assert reply.body == ()  # empty method-return

        await asyncio.sleep(0.2)
        assert len(received) == 1
        window, screens = received[0]
        assert window.rect == Rect(0, 0, 800, 600)
        assert screens[0].name == "DP-1"
        # a real UpdateState call counts as activity -> presence stays Active
        assert bridge.state == PresenceState.ACTIVE
    finally:
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task


@pytest.mark.skipif(not HAS_SESSION_BUS, reason="needs a live D-Bus session bus")
@pytest.mark.asyncio
async def test_fake_screensaver_signal_source_updates_presence():
    """The real org.freedesktop.ScreenSaver name is owned by kwin_wayland
    and can't be claimed twice, so this stands in a second,
    differently-named D-Bus service implementing the same signal shape and
    points the bridge's internal watcher logic at it directly -- exercising
    the actual AddMatch + signal-decode path against a real bus, just not
    the real well-known name."""
    from jeepney import DBusAddress, new_method_return
    from jeepney.bus_messages import MatchRule, message_bus
    from jeepney.io.asyncio import Proxy, open_dbus_router
    from jeepney.wrappers import new_signal

    fake_name = "org.deskmate.FakeScreenSaverTest"
    fake_addr = DBusAddress("/org/freedesktop/ScreenSaver", bus_name=fake_name, interface="org.freedesktop.ScreenSaver")

    import brain.presence.desktop_bridge as module

    original = module._SCREENSAVER_SERVICE
    module._SCREENSAVER_SERVICE = fake_addr
    watcher_task = None
    try:
        # The fake service must claim its name and be ready to answer
        # GetActive() *before* the bridge's watcher starts, or its startup
        # call finds no owner, fails, and the watcher gives up for good
        # (the documented degrade-gracefully behavior) before we're listening.
        async with open_dbus_router(bus="SESSION") as fake_router:
            bus = Proxy(message_bus, fake_router)
            (result,) = await bus.RequestName(fake_name)
            assert result in (1, 2)

            rule = MatchRule(type="method_call", interface=fake_addr.interface, path=fake_addr.object_path)
            with fake_router.filter(rule) as queue:
                bridge = DesktopPresenceBridge(
                    "org.deskmate.PresenceTest2",
                    "/org/deskmate/PresenceTest2",
                    idle_after_s=600,
                    on_update=lambda *_: None,
                )
                watcher_task = asyncio.create_task(bridge._run_screensaver_watcher())

                msg = await asyncio.wait_for(queue.get(), timeout=2)  # the bridge's GetActive() call
                await fake_router.send(new_method_return(msg, signature="b", body=(False,)))

            await asyncio.sleep(0.2)
            assert bridge.state == PresenceState.ACTIVE

            emit = new_signal(fake_addr, "ActiveChanged", "b", (True,))
            await fake_router.send(emit)

            await asyncio.sleep(0.3)
        assert bridge.state == PresenceState.AWAY
    finally:
        module._SCREENSAVER_SERVICE = original
        if watcher_task is not None:
            watcher_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher_task


# --- live D-Bus integration: real login1 session (SYSTEM bus) ---------------


@pytest.mark.skipif(not HAS_LOGIN1_SESSION, reason="needs XDG_SESSION_ID and busctl")
@pytest.mark.asyncio
async def test_real_login1_idle_hint_signal_updates_presence():
    """No fake service needed here, unlike ScreenSaver -- SetIdleHint is a
    real method on our own real login1 session object that an ordinary
    user is allowed to call, so this toggles the actual IdleHint property
    on the actual session this test runs in and confirms the bridge's real
    AddMatch subscription on the SYSTEM bus picks up the resulting real
    PropertiesChanged signal.

    Asserts on the raw `_idle_hint` flag rather than the overall
    `PresenceState`: the session's *actual* LockedHint can legitimately be
    true at test time (e.g. the session actually idled/locked during a
    long-running test run), which would make an assertion like
    "state == ACTIVE before toggling" flaky for reasons that have nothing
    to do with whether this code works. The flag itself is what this test
    exists to prove gets updated from a real signal."""
    def _busctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["busctl", *args], capture_output=True, text=True, check=check)

    session_id = os.environ["XDG_SESSION_ID"]
    path_result = await asyncio.to_thread(
        _busctl, "call", "org.freedesktop.login1", "/org/freedesktop/login1",
        "org.freedesktop.login1.Manager", "GetSession", "s", session_id,
    )
    session_path = path_result.stdout.split('"')[1]

    bridge = DesktopPresenceBridge(
        "org.deskmate.Login1Test", "/org/deskmate/Login1Test", idle_after_s=600, on_update=lambda *_: None
    )
    watcher_task = asyncio.create_task(bridge._run_login1_watcher())
    try:
        await asyncio.sleep(0.5)
        assert bridge._idle_hint is False  # real session's actual starting value, read at startup

        await asyncio.to_thread(
            _busctl, "call", "org.freedesktop.login1", session_path,
            "org.freedesktop.login1.Session", "SetIdleHint", "b", "true",
        )
        await asyncio.sleep(0.5)
        assert bridge._idle_hint is True
        assert bridge.state == PresenceState.AWAY  # idle_hint alone is enough to force Away
    finally:
        await asyncio.to_thread(
            _busctl, "call", "org.freedesktop.login1", session_path,
            "org.freedesktop.login1.Session", "SetIdleHint", "b", "false", check=False,
        )
        watcher_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher_task
