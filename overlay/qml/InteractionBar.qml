// A 48px-tall bar, as wide as the avatar, tucked just under its feet (see
// Main.qml): a single-line text input that takes the remaining width plus a
// 36x36 push-to-talk button.
import QtQuick
import QtQuick.Controls.Basic

Rectangle {
    id: root
    width: 240  // fallback only; Main.qml sets it to the configured avatar width
    height: 48
    // Opaque with a light border: the bar floats over arbitrary windows
    // (often dark ones), and a dark translucent fill vanished against them.
    color: "#f21e1e28"
    radius: 10
    border.color: "#9a9ac0"
    border.width: 2

    property string pushToTalkMode: "hold"

    signal textSubmitted(string text)
    signal micPressed()
    signal micReleased()
    signal micToggled()

    function focusInput() {
        textField.forceActiveFocus()
    }

    Row {
        anchors.fill: parent
        anchors.margins: 6
        spacing: 6

        TextField {
            id: textField
            width: parent.width - micButton.width - parent.spacing
            height: parent.height
            placeholderText: "Say something..."
            color: "white"
            placeholderTextColor: "#9090a0"
            background: Rectangle { color: "#3a3a52"; radius: 6 }
            onAccepted: {
                if (text.length > 0) {
                    root.textSubmitted(text)
                    text = ""
                }
            }
        }

        Rectangle {
            id: micButton
            width: 36
            height: 36
            radius: 18
            color: micArea.pressed ? "#c0392b" : "#3a3a46"

            // Plain text placeholder glyph -- a real icon asset is a
            // follow-on polish item, not functionally blocking.
            Text {
                anchors.centerIn: parent
                text: "PTT"
                font.pixelSize: 9
                color: "white"
            }

            MouseArea {
                id: micArea
                anchors.fill: parent
                onPressed: if (root.pushToTalkMode === "hold") root.micPressed()
                onReleased: if (root.pushToTalkMode === "hold") root.micReleased()
                onClicked: if (root.pushToTalkMode === "toggle") root.micToggled()
            }
        }
    }
}
