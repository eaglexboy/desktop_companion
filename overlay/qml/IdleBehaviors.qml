// Layered idle/gesture animation tiers, implemented as QML property
// animations directly on the loaded model's QQuick3DNode transform (not by
// replaying baked armature clips -- see avatar_controller.h's scope note
// for why). Delivery-style (talk_*) blending is driven from Main.qml
// alongside speech playback, not here.
import QtQuick
import QtQuick3D

Item {
    id: root
    property Node avatar: null
    property string mood: "neutral"
    property bool thinking: false
    // Faceless-mode (has_face:false) speech indicator: a fixed ~4.5Hz
    // scale pulse, gated by playback state rather than true real-time
    // audio RMS -- Qt Multimedia's QMediaPlayer exposes no RMS/amplitude
    // tap without a lower-level QAudioSink pipeline.
    property bool pulsing: false
    onPulsingChanged: if (!pulsing && root.avatar) root.avatar.scale.y = 1.0

    SequentialAnimation {
        id: pulseLoop
        loops: Animation.Infinite
        running: root.avatar !== null && root.pulsing
        NumberAnimation { target: root.avatar; property: "scale.y"; from: 1.0; to: 1.08; duration: 111; easing.type: Easing.InOutSine }
        NumberAnimation { target: root.avatar; property: "scale.y"; from: 1.08; to: 1.0; duration: 111; easing.type: Easing.InOutSine }
    }

    // Evasion motion. The baked `move` clip is stripped at load (see
    // Avatar3D.qml's GlbSanitizer), so while the surface slides across the
    // screen the model hops and leans into the direction of travel.
    property bool moving: false
    property int travelDir: 1   // +1 right, -1 left
    SequentialAnimation {
        id: hopLoop
        loops: Animation.Infinite
        running: root.avatar !== null && root.moving
        NumberAnimation { target: root.avatar; property: "position.y"; to: 0.14; duration: 110; easing.type: Easing.OutQuad }
        NumberAnimation { target: root.avatar; property: "position.y"; to: 0; duration: 110; easing.type: Easing.InQuad }
        onRunningChanged: if (!running && root.avatar) root.avatar.position.y = 0
    }
    NumberAnimation {
        id: leanAnim
        target: root.avatar
        property: "eulerRotation.z"
        duration: 120
    }
    onMovingChanged: {
        if (!root.avatar || root.thinking)
            return
        leanAnim.to = root.moving ? -9 * root.travelDir : 0
        leanAnim.restart()
    }

    readonly property var moodPresets: ({
        "neutral": { amplitude: 4, periodMs: 4000 },
        "bored": { amplitude: 2, periodMs: 6000 },
        "curious": { amplitude: 6, periodMs: 2800 },
        "fidget": { amplitude: 8, periodMs: 1600 },
        "content": { amplitude: 3, periodMs: 4500 }
    })

    readonly property var activePreset: moodPresets[mood] || moodPresets["neutral"]

    // Base idle loop + mood idle, collapsed into one continuous sway whose
    // amplitude/speed is selected by `mood` -- visually equivalent to
    // switching between separately-named idle clips.
    SequentialAnimation {
        id: idleLoop
        loops: Animation.Infinite
        running: root.avatar !== null && !root.thinking
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.y"
            from: -root.activePreset.amplitude
            to: root.activePreset.amplitude
            duration: root.activePreset.periodMs
            easing.type: Easing.InOutSine
        }
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.y"
            from: root.activePreset.amplitude
            to: -root.activePreset.amplitude
            duration: root.activePreset.periodMs
            easing.type: Easing.InOutSine
        }
    }

    // Cognitive "think" loop, active exactly while `thinking` is true. The
    // binding on idleLoop.running above means this interrupts the base
    // idle immediately.
    SequentialAnimation {
        id: thinkLoop
        loops: Animation.Infinite
        running: root.avatar !== null && root.thinking
        NumberAnimation { target: root.avatar; property: "eulerRotation.z"; from: -3; to: 3; duration: 900; easing.type: Easing.InOutSine }
        NumberAnimation { target: root.avatar; property: "eulerRotation.z"; from: 3; to: -3; duration: 900; easing.type: Easing.InOutSine }
    }

    // One-shot auxiliary reactions (`play_gesture` events). Names not in
    // this table are cleanly ignored -- this is a small built-in
    // transform-based reaction set rather than a baked clip lookup. All
    // properties here are rotations, deliberately -- gestureAnim's second
    // step always returns to 0, which is only correct for a rotation's
    // neutral pose, not e.g. a scale's (1.0).
    readonly property var gesturePresets: ({
        "greet": { property: "eulerRotation.y", to: 25, duration: 260 },
        "agree": { property: "eulerRotation.x", to: -12, duration: 200 },
        "disagree": { property: "eulerRotation.y", to: 15, duration: 150 },
        "celebrate": { property: "eulerRotation.x", to: 10, duration: 180 },
        "shrug": { property: "eulerRotation.z", to: 8, duration: 220 }
    })

    function playGesture(name) {
        const preset = gesturePresets[name]
        if (!preset || !root.avatar)
            return
        gestureAnim.stop()
        gestureAnim.targetProperty = preset.property
        gestureAnim.toValue = preset.to
        gestureAnim.stepDuration = preset.duration
        gestureAnim.start()
    }

    SequentialAnimation {
        id: gestureAnim
        property string targetProperty: ""
        property real toValue: 0
        property int stepDuration: 200

        NumberAnimation { target: root.avatar; property: gestureAnim.targetProperty; to: gestureAnim.toValue; duration: gestureAnim.stepDuration; easing.type: Easing.OutQuad }
        NumberAnimation { target: root.avatar; property: gestureAnim.targetProperty; to: 0; duration: gestureAnim.stepDuration; easing.type: Easing.InQuad }
    }
}
