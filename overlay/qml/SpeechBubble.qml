// A click-through text bubble that follows the avatar and shows what is being
// said. It is its own layer-shell surface rather than part of the avatar's
// window: the avatar window must stay exactly the avatar footprint (daemon
// placement, the daemon's own-window rejection, and the input bar all depend
// on that), and a bubble needs room above or below it.
import QtQuick
import QtQuick.Window
import org.kde.layershell as LayerShellQt
import Deskmate

Window {
    id: bubble

    // --- inputs, set by Main.qml ------------------------------------------
    property string text: ""
    property bool enabled: true      // [presence] show_speech_bubble
    property bool avatarHidden: false
    // Fallbacks only; Main.qml binds these to the configured avatar size.
    property int avatarWidth: 240
    property int avatarHeight: 240

    // --- geometry, set via place() ----------------------------------------
    property string anchoredScreenName: ""
    property real avatarLeft: 0      // avatar position relative to its output
    property real avatarTop: 0
    property real outputWidth: 1920
    property real outputHeight: 1080
    property bool suspended: false   // true only while re-homing to another output

    readonly property int gap: 6
    readonly property int bubbleWidth: Math.max(avatarWidth + 40, 260)
    readonly property real wantLeft: Math.min(Math.max(avatarLeft + (avatarWidth - bubbleWidth) / 2, 0), Math.max(outputWidth - bubbleWidth, 0))
    // Above the avatar when it fits, otherwise below it (avatar near the top of the screen).
    readonly property bool above: avatarTop - height - gap >= 0

    width: bubbleWidth
    height: body.height
    color: "transparent"
    flags: Qt.FramelessWindowHint
    transientParent: null   // a layer-shell surface cannot have a parent window
    visible: enabled && !suspended && !avatarHidden && text !== ""

    LayerShellQt.Window.layer: LayerShellQt.Window.LayerOverlay
    LayerShellQt.Window.anchors: LayerShellQt.Window.AnchorTop | LayerShellQt.Window.AnchorLeft
    LayerShellQt.Window.exclusionZone: -1
    LayerShellQt.Window.keyboardInteractivity: LayerShellQt.Window.KeyboardInteractivityNone
    LayerShellQt.Window.margins.left: wantLeft
    LayerShellQt.Window.margins.top: above ? avatarTop - height - gap : avatarTop + avatarHeight + gap

    WindowMaskHelper { id: maskHelper }
    LayerShellScreenHelper { id: layerShellScreen }

    // Click-through: a 1x1 input region. (An *empty* mask means "no mask" to
    // Qt, i.e. the whole window would swallow clicks.) The mask is dropped
    // whenever the surface is recreated, so re-apply on every show.
    onVisibleChanged: if (visible) maskHelper.applyMask(bubble, 1, 1)

    // Re-homing to another output = destroy surface, repoint, recreate. The
    // `suspended` flag (rather than assigning `visible`) keeps the visible
    // binding intact.
    function place(screenName, left, top, outW, outH) {
        avatarLeft = left
        avatarTop = top
        outputWidth = outW
        outputHeight = outH
        if (anchoredScreenName !== screenName) {
            suspended = true
            layerShellScreen.moveToScreen(bubble, screenName)
            anchoredScreenName = screenName
            suspended = false
        }
    }

    Rectangle {
        id: body
        width: parent.width
        height: label.implicitHeight + 20
        radius: 12
        color: "#f4f4fa"
        border.color: "#9a9ac0"
        border.width: 2

        Text {
            id: label
            x: 12
            y: 10
            width: parent.width - 24
            text: bubble.text
            wrapMode: Text.Wrap
            color: "#1e1e28"
            font.pixelSize: 14
            lineHeight: 1.1
        }
    }
}
