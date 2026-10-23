"""D-Bus service that adapters/kwin/contents/code/main.js pushes window/
screen/desktop state to via callDBus(), plus a User_Active/User_Idle/
User_Away presence state machine. Runs as a background asyncio task inside
the brain daemon (see brain/main.py).

Three independent D-Bus connections are involved:

1. A SESSION-bus *server* connection claiming `org.deskmate.Presence` and
   receiving `UpdateState` method calls from the KWin script.
2. A SESSION-bus *client* connection subscribing to
   `org.freedesktop.ScreenSaver`'s `ActiveChanged(b)` signal -- on
   KWin/Wayland this interface is implemented by `kwin_wayland` itself, no
   separate screen-locker process.
3. A SYSTEM-bus *client* connection subscribing to
   `org.freedesktop.DBus.Properties.PropertiesChanged` on this login
   session's own `org.freedesktop.login1.Session` object, for
   `LockedHint`/`IdleHint`. Getting "our own session's object path" is the
   one genuinely tricky part: `Manager.GetSessionByPID(os.getpid())` --
   the obvious-looking approach -- fails for any PID that isn't literally
   the session's registered "Leader" PID (including the compositor's own
   PID, not just an unrelated process). The approach that actually works:
   read `$XDG_SESSION_ID` from the environment and call
   `Manager.GetSession(id)` -- that variable is present in a systemd
   --user unit's environment on a real login session, which is the
   deployment shape `deploy/systemd/deskmate-brain.service` uses. If the
   env var is absent (e.g. a non-systemd-user launch, or logind isn't
   managing the session at all), this degrades per the rule below rather
   than guessing a session path.

Both client-side signal subscriptions (2 and 3) need an explicit `AddMatch`
sent to the bus -- unlike the server connection's own incoming method calls,
these are *other* connections' signals, which jeepney's local-only
`router.filter()` can't see unless the bus is told to route them to us.

**Degradation rule:** if the session bus, ScreenSaver, or login1 are
unavailable for any reason, presence defaults to `User_Active` always
(never silently assumes Away), and logs once per missing source rather
than retrying forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from jeepney import (
    DBusAddress,
    HeaderFields,
    MessageFlag,
    new_method_call,
    new_method_return,
)
from jeepney.bus_messages import MatchRule, message_bus
from jeepney.io.asyncio import Proxy, open_dbus_router
from jeepney.wrappers import Properties

from brain.presence.placement import Rect, ScreenInfo, WindowState

logger = logging.getLogger("deskmate.presence.desktop_bridge")

UpdateCallback = Callable[[WindowState | None, list[ScreenInfo]], Awaitable[None]]

# RequestName reply codes (org.freedesktop.DBus): 1 = became primary owner,
# 2 = queued (another instance already owns the name).
_NAME_ACQUIRED = 1
_NAME_QUEUED = 2

_SCREENSAVER_SERVICE = DBusAddress(
    "/org/freedesktop/ScreenSaver", bus_name="org.freedesktop.ScreenSaver", interface="org.freedesktop.ScreenSaver"
)
_LOGIN1_MANAGER = DBusAddress(
    "/org/freedesktop/login1", bus_name="org.freedesktop.login1", interface="org.freedesktop.login1.Manager"
)
_LOGIN1_SESSION_IFACE = "org.freedesktop.login1.Session"

# How often the idle ticker re-checks seconds_since_activity against
# idle_after_s -- there's no other event that fires exactly at that
# boundary, so something has to poll for it.
_IDLE_TICK_S = 5.0


# --- pure state machine (no D-Bus, no I/O) -----------------------------------


class PresenceState(StrEnum):
    ACTIVE = "User_Active"
    IDLE = "User_Idle"
    AWAY = "User_Away"


@dataclass(frozen=True)
class PresenceSignals:
    screensaver_active: bool
    locked_hint: bool
    idle_hint: bool
    seconds_since_activity: float


TransitionCallback = Callable[[PresenceState, PresenceState], Awaitable[None]]


class PresenceStateMachine:
    """Folds (screensaver_active, locked_hint, idle_hint,
    seconds_since_activity) into one of three states:

    * User_Away: screensaver active, session locked, or "display sleeping"
      -- the last of which has no separate signal of its own, so login1's
      own IdleHint (its documented meaning is session-controller-managed
      idle, typically tied to triggering a screen/suspend action) is used
      as that proxy rather than inventing a fourth input.
    * User_Idle: none of the above, but no window/pointer-change activity
      for > idle_after_s.
    * User_Active: otherwise.

    Pure and D-Bus-free so the full transition table is unit-testable
    without touching any bus.
    """

    def __init__(self, idle_after_s: float, on_transition: TransitionCallback | None = None) -> None:
        self._idle_after_s = idle_after_s
        self._on_transition = on_transition
        self._state = PresenceState.ACTIVE

    @property
    def state(self) -> PresenceState:
        return self._state

    def _compute(self, signals: PresenceSignals) -> PresenceState:
        if signals.screensaver_active or signals.locked_hint or signals.idle_hint:
            return PresenceState.AWAY
        if signals.seconds_since_activity > self._idle_after_s:
            return PresenceState.IDLE
        return PresenceState.ACTIVE

    async def update(self, signals: PresenceSignals) -> PresenceState:
        new_state = self._compute(signals)
        if new_state != self._state:
            old_state = self._state
            self._state = new_state
            if self._on_transition is not None:
                await self._on_transition(old_state, new_state)
        return self._state


# --- KWin telemetry parsing ---------------------------------------------


def _parse_rect(d: dict) -> Rect:
    return Rect(int(d["x"]), int(d["y"]), int(d["width"]), int(d["height"]))


def _parse_payload(raw: str) -> tuple[WindowState | None, list[ScreenInfo]]:
    """Expected JSON shape, produced by the KWin script:
    {
      "window": {"x":.., "y":.., "width":.., "height":.., "screen": "DP-1",
                 "fullscreen": bool, "desktop": int} | null,
      "screens": [{"name": "DP-1", "x":.., "y":.., "width":.., "height":..}, ...]
    }
    """
    payload = json.loads(raw)
    screens = [ScreenInfo(s["name"], _parse_rect(s)) for s in payload.get("screens", [])]
    win_data = payload.get("window")
    window = None
    if win_data:
        window = WindowState(
            rect=_parse_rect(win_data),
            screen_name=win_data.get("screen"),
            fullscreen=bool(win_data.get("fullscreen", False)),
            desktop=int(win_data.get("desktop", 1)),
        )
    return window, screens


class DesktopPresenceBridge:
    """Owns all three D-Bus connections for the daemon's lifetime. Await
    `run()` as a background task; it runs until cancelled. The KWin
    telemetry server is the one connection this class requires to work at
    all; the ScreenSaver and login1 watchers are each independently
    optional -- either one failing to set up degrades presence detection
    (falls back to never reporting Away from that source) without taking
    down the others."""

    def __init__(
        self,
        service_name: str,
        object_path: str,
        idle_after_s: float,
        on_update: UpdateCallback,
        on_transition: TransitionCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.address = DBusAddress(object_path, bus_name=service_name, interface=service_name)
        self.on_update = on_update
        self._clock = clock
        self._state_machine = PresenceStateMachine(idle_after_s, on_transition=on_transition)
        self._screensaver_active = False
        self._locked_hint = False
        self._idle_hint = False
        self._last_activity = clock()

    @property
    def state(self) -> PresenceState:
        return self._state_machine.state

    async def run(self) -> None:
        await asyncio.gather(
            self._run_kwin_server(),
            self._run_screensaver_watcher(),
            self._run_login1_watcher(),
            self._run_idle_ticker(),
        )

    async def _reevaluate(self) -> None:
        signals = PresenceSignals(
            screensaver_active=self._screensaver_active,
            locked_hint=self._locked_hint,
            idle_hint=self._idle_hint,
            seconds_since_activity=self._clock() - self._last_activity,
        )
        await self._state_machine.update(signals)

    # --- source 1: KWin telemetry server (required) -------------------------

    async def _run_kwin_server(self) -> None:
        async with open_dbus_router(bus="SESSION") as router:
            bus = Proxy(message_bus, router)
            (result,) = await bus.RequestName(self.address.bus_name)
            if result == _NAME_ACQUIRED:
                logger.info("claimed D-Bus name %s", self.address.bus_name)
            elif result == _NAME_QUEUED:
                logger.warning(
                    "D-Bus name %s already owned by another process (maybe a previous "
                    "deskmate instance still running?) -- queued, will only get it if that "
                    "one releases it",
                    self.address.bus_name,
                )
            else:
                logger.error("could not claim D-Bus name %s (result=%s)", self.address.bus_name, result)

            rule = MatchRule(type="method_call", interface=self.address.interface, path=self.address.object_path)
            with router.filter(rule) as queue:
                logger.info(
                    "desktop presence service listening on %s%s", self.address.bus_name, self.address.object_path
                )
                while True:
                    msg = await queue.get()
                    await self._handle_kwin_call(router, msg)

    async def _handle_kwin_call(self, router, msg) -> None:
        member = msg.header.fields.get(HeaderFields.member)
        try:
            if member == "UpdateState":
                (raw,) = msg.body
                window, screens = _parse_payload(raw)
                self._last_activity = self._clock()
                await self._reevaluate()
                await self.on_update(window, screens)
            else:
                logger.debug("ignoring unknown D-Bus method call: %s", member)
        except Exception:
            logger.exception("error handling a kwin-script update")
        finally:
            if not (msg.header.flags & MessageFlag.no_reply_expected):
                await router.send(new_method_return(msg))

    # --- source 2: ScreenSaver.ActiveChanged (optional) ----------------------

    async def _run_screensaver_watcher(self) -> None:
        try:
            async with open_dbus_router(bus="SESSION") as router:
                proxy = Proxy(message_bus, router)
                # Deliberately no sender= here: the bus delivers correctly
                # either way, but jeepney's own *local* re-check
                # (MatchRule.matches(), run again against every delivered
                # message) compares sender against the message's actual
                # delivered sender field, which is always the owning
                # connection's unique name (":1.123"), never the well-known
                # name we'd naturally write here -- so a sender= filter on a
                # well-known name silently never matches locally, even
                # though the bus-level AddMatch routing worked.
                # interface+member is already specific enough for this signal.
                rule = MatchRule(type="signal", interface=_SCREENSAVER_SERVICE.interface, member="ActiveChanged")
                await proxy.AddMatch(rule)

                # GetActive is a plain method on this interface (not a
                # standard D-Bus property), so it's called directly.
                reply = await router.send_and_get_reply(new_method_call(_SCREENSAVER_SERVICE, "GetActive"))
                (self._screensaver_active,) = reply.body
                await self._reevaluate()

                with router.filter(rule) as queue:
                    logger.info("subscribed to %s ActiveChanged", _SCREENSAVER_SERVICE.interface)
                    while True:
                        msg = await queue.get()
                        (self._screensaver_active,) = msg.body
                        await self._reevaluate()
        except Exception:
            logger.warning(
                "could not subscribe to %s -- presence will never report Away from the "
                "screensaver signal (still may via login1 or stay Active)",
                _SCREENSAVER_SERVICE.interface,
                exc_info=True,
            )

    # --- source 3: login1 Session LockedHint/IdleHint (optional) ------------

    async def _run_login1_watcher(self) -> None:
        session_id = os.environ.get("XDG_SESSION_ID")
        if not session_id:
            logger.warning("XDG_SESSION_ID is not set -- cannot subscribe to login1 LockedHint/IdleHint")
            return
        try:
            async with open_dbus_router(bus="SYSTEM") as router:
                reply = await router.send_and_get_reply(
                    new_method_call(_LOGIN1_MANAGER, "GetSession", "s", (session_id,))
                )
                (session_path,) = reply.body
                session_addr = DBusAddress(
                    session_path, bus_name="org.freedesktop.login1", interface=_LOGIN1_SESSION_IFACE
                )
                properties = Properties(session_addr)

                for prop_name, attr in (("LockedHint", "_locked_hint"), ("IdleHint", "_idle_hint")):
                    prop_reply = await router.send_and_get_reply(properties.get(prop_name))
                    (variant,) = prop_reply.body
                    setattr(self, attr, variant[1])
                await self._reevaluate()

                # No sender= here either -- see _run_screensaver_watcher's
                # comment. path= (this exact session object) is already
                # specific enough.
                rule = MatchRule(
                    type="signal",
                    interface="org.freedesktop.DBus.Properties",
                    member="PropertiesChanged",
                    path=session_path,
                )
                proxy = Proxy(message_bus, router)
                await proxy.AddMatch(rule)

                with router.filter(rule) as queue:
                    logger.info("subscribed to login1 session %s PropertiesChanged", session_path)
                    while True:
                        msg = await queue.get()
                        _interface_name, changed, _invalidated = msg.body
                        if "LockedHint" in changed:
                            self._locked_hint = changed["LockedHint"][1]
                        if "IdleHint" in changed:
                            self._idle_hint = changed["IdleHint"][1]
                        if "LockedHint" in changed or "IdleHint" in changed:
                            await self._reevaluate()
        except Exception:
            logger.warning(
                "could not subscribe to login1 session %s -- presence will never report "
                "Away from LockedHint/IdleHint (still may via the screensaver signal or "
                "stay Active)",
                session_id,
                exc_info=True,
            )

    # --- source 4: idle-with-no-signal ticker --------------------------------

    async def _run_idle_ticker(self) -> None:
        while True:
            await asyncio.sleep(_IDLE_TICK_S)
            await self._reevaluate()
