// Deskmate presence telemetry adapter for KWin (KDE Plasma 6).
//
// Forwards active-window geometry and screen layout to the Deskmate brain
// daemon's org.deskmate.Presence D-Bus service
// (brain/presence/desktop_bridge.py), which consumes it to decide
// non-intrusive avatar placement.

var sendTimer = null;
var hookedWindows = {};

function debouncedSend() {
    if (sendTimer) sendTimer.destroy();
    sendTimer = new QTimer();
    sendTimer.interval = 100;
    sendTimer.singleShot = true;
    sendTimer.timeout.connect(sendState);
    sendTimer.start();
}

// Compares class/name/desktop-file identifiers only, case-insensitively and
// treating "_" like "-" (the binary is deskmate_overlay, the app name is
// deskmate-overlay, and which one a layer-shell surface reports is not
// guaranteed). The window title is deliberately NOT consulted: other
// windows legitimately mention "Deskmate" in theirs. The brain daemon also
// ignores telemetry matching the avatar's own last-placed rectangle, so a
// miss here cannot cause the avatar to flee its own window.
function isOverlayWindow(win) {
    var ids = [win.resourceClass, win.resourceName, win.desktopFileName];
    for (var i = 0; i < ids.length; ++i) {
        if (ids[i] && String(ids[i]).toLowerCase().replace(/_/g, "-") === "deskmate-overlay")
            return true;
    }
    return false;
}

// The overlay re-creates its layer-shell surface to change monitors, and
// KWin's close animation (fadingpopups) would keep the old copy fading out
// after the new one has appeared on the other monitor. The companion runs
// off one edge and in at the other, so the old surface must just vanish.
function skipOverlayAnimation(win) {
    if (win && isOverlayWindow(win))
        win.skipsCloseAnimation = true;
}
workspace.windowAdded.connect(skipOverlayAnimation);
workspace.windowList().forEach(skipOverlayAnimation);

function sendState() {
    var active = workspace.activeWindow;

    // Invariant: If the active window is the overlay itself, DO NOT send telemetry.
    // Emitting window: null causes the avatar to flee its own input box.
    if (active && isOverlayWindow(active)) {
        return;
    }

    var windowData = null;
    if (active && !active.specialWindow && !active.minimized) {
        windowData = {
            x: Math.round(active.x),
            y: Math.round(active.y),
            width: Math.round(active.width),
            height: Math.round(active.height),
            screen: active.output ? active.output.name : "",
            fullscreen: active.fullScreen,
            desktop: workspace.currentDesktop ? workspace.currentDesktop.x11DesktopNumber : 1
        };
    }

    var screensData = [];
    var screens = workspace.screens;
    for (var i = 0; i < screens.length; ++i) {
        var s = screens[i];
        screensData.push({
            name: s.name,
            x: Math.round(s.geometry.x),
            y: Math.round(s.geometry.y),
            width: Math.round(s.geometry.width),
            height: Math.round(s.geometry.height)
        });
    }

    callDBus(
        "org.deskmate.Presence",
        "/org/deskmate/Presence",
        "org.deskmate.Presence",
        "UpdateState",
        JSON.stringify({ window: windowData, screens: screensData })
    );
}

workspace.windowActivated.connect(function(win) {
    if (win && !hookedWindows[win.internalId]) {
        hookedWindows[win.internalId] = true;
        win.frameGeometryChanged.connect(debouncedSend);
        win.fullScreenChanged.connect(debouncedSend);
    }
    debouncedSend();
});

workspace.windowRemoved.connect(function(win) {
    if (win && hookedWindows[win.internalId]) {
        delete hookedWindows[win.internalId];
    }
    debouncedSend();
});

workspace.screensChanged.connect(debouncedSend);
workspace.currentDesktopChanged.connect(debouncedSend);

// Dispatch initial state
debouncedSend();
