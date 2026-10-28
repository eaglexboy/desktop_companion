// A small pill at the top of the avatar showing the progress of an
// appearance change (brain/evolution.py stages: designing -> building ->
// done | staged | failed). It lives inside the avatar window, which is
// already click-through outside the avatar, so it needs no surface of its own.
import QtQuick

Rectangle {
    id: pill

    // Set from Main.qml on every `appearance_progress` event.
    property string stage: ""
    property string label: ""
    property real startedMs: 0
    property int elapsedS: 0

    readonly property bool working: stage === "designing" || stage === "building"
    readonly property int step: stage === "designing" ? 1 : stage === "building" ? 2 : 0

    function update(payload) {
        // the clock covers the whole change, so only a fresh design (or a
        // build with no design step, e.g. the REST API) restarts it
        if (payload.stage === "designing" || !working)
            startedMs = Date.now()
        stage = payload.stage || ""
        label = payload.label || ""
        elapsedS = Math.floor((Date.now() - startedMs) / 1000)
        if (working) {
            hideTimer.stop()
        } else {
            // terminal stage: linger longer on failure so it can be read
            hideTimer.interval = stage === "failed" ? 6000 : 3000
            hideTimer.restart()
        }
        shown = true
    }

    property bool shown: false
    visible: opacity > 0
    opacity: shown ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 200 } }

    anchors.horizontalCenter: parent.horizontalCenter
    anchors.top: parent.top
    anchors.topMargin: 4
    width: Math.min(parent.width - 16, text.implicitWidth + dot.width + 28)
    height: 26
    radius: 13
    color: stage === "failed" ? "#fdecea" : "#f4f4fa"
    border.width: 1
    border.color: stage === "failed" ? "#c0392b" : stage === "done" ? "#3b9a5a" : "#9a9ac0"

    Rectangle {
        id: dot
        width: 8; height: 8; radius: 4
        anchors.left: parent.left
        anchors.leftMargin: 10
        anchors.verticalCenter: parent.verticalCenter
        color: pill.stage === "failed" ? "#c0392b" : pill.stage === "done" ? "#3b9a5a" : "#6a6ad0"
        SequentialAnimation on opacity {
            running: pill.working
            loops: Animation.Infinite
            NumberAnimation { to: 0.25; duration: 500 }
            NumberAnimation { to: 1.0; duration: 500 }
        }
    }

    Text {
        id: text
        anchors.left: dot.right
        anchors.leftMargin: 6
        anchors.right: parent.right
        anchors.rightMargin: 8
        anchors.verticalCenter: parent.verticalCenter
        elide: Text.ElideRight
        color: "#1e1e28"
        font.pixelSize: 11
        text: pill.working
            ? pill.label + "  (" + pill.step + "/2 · " + pill.elapsedS + "s)"
            : pill.label
    }

    Timer {
        interval: 1000
        repeat: true
        running: pill.working
        onTriggered: pill.elapsedS = Math.floor((Date.now() - pill.startedMs) / 1000)
    }
    Timer {
        id: hideTimer
        onTriggered: pill.shown = false
    }
}
