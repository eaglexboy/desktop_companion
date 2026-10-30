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
    // AvatarController: drives the eye blink morph targets.
    property var controller: null
    property bool thinking: false
    // Faceless-mode (has_face:false) speech indicator: a fixed ~4.5Hz
    // scale pulse, gated by playback state rather than true real-time
    // audio RMS -- Qt Multimedia's QMediaPlayer exposes no RMS/amplitude
    // tap without a lower-level QAudioSink pipeline.
    property bool pulsing: false
    onPulsingChanged: {
        if (pulsing)
            cancelMicroIdle()
        else if (root.avatar)
            root.avatar.scale.y = 1.0
    }

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
    // Running legs. Avatars with legs carry `run_a`/`run_b` morph targets
    // (the two mirrored extremes of a stride); cross-fading them in time with
    // the hop makes the body actually run. Avatars without them keep the
    // plain hop-and-lean above.
    property bool canRun: false
    property real runAmount: 0     // 0 = standing, 1 = full stride
    property real runPhase: 0      // 0..1 across one two-step cycle
    Behavior on runAmount { NumberAnimation { duration: 140 } }
    function applyRunPose() {
        if (!root.controller)
            return
        // Extremes land at phase 0.25 / 0.75 = the top of each hop, which is
        // when a runner's stride is widest (the flight phase).
        const k = 0.5 + 0.5 * Math.sin(2 * Math.PI * root.runPhase)
        root.controller.setRunWeights(root.runAmount * k, root.runAmount * (1 - k))
    }
    onRunAmountChanged: applyRunPose()
    onRunPhaseChanged: applyRunPose()
    NumberAnimation {
        id: runCycle
        target: root
        property: "runPhase"
        from: 0
        to: 1
        duration: 440   // two hops (110ms up + 110ms down each)
        loops: Animation.Infinite
        running: root.avatar !== null && (root.moving && root.canRun || root.runAmount > 0)
    }

    onMovingChanged: {
        if (moving) {
            cancelMicroIdle()
            // Checked per trip: a hot-reloaded look replaces the nodes.
            root.canRun = root.controller !== null && root.controller.hasRunTargets()
        }
        root.runAmount = (moving && root.canRun) ? 1 : 0
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
        running: root.avatar !== null && !root.thinking && !root.swayPaused
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


    // --- Tier 1/2 idle variety ------------------------------------------
    // Every so often the avatar performs one short, randomly chosen idle
    // (look around, stretch, peek, sleepy nod, fidget) on top of the
    // continuous sway so it doesn't look like a single looping motion. Which
    // ones are likely depends on `mood`. These are transform-based stand-ins
    // for the baked idle_* clips, which the overlay never plays.
    property bool swayPaused: false   // look-around owns eulerRotation.y while it runs

    readonly property var microWeights: ({
        "neutral": { lookaround: 3, stretch: 2, peek: 2, nod: 1, fidget: 1 },
        "bored": { lookaround: 3, stretch: 3, peek: 1, nod: 3, fidget: 1 },
        "curious": { lookaround: 4, stretch: 1, peek: 4, nod: 0, fidget: 1 },
        "fidget": { lookaround: 2, stretch: 1, peek: 1, nod: 0, fidget: 5 },
        "content": { lookaround: 2, stretch: 3, peek: 2, nod: 2, fidget: 0 }
    })

    readonly property bool microRunning: lookAroundAnim.running || stretchAnim.running
        || peekAnim.running || nodAnim.running || fidgetAnim.running

    function pickMicroIdle() {
        const weights = microWeights[mood] || microWeights["neutral"]
        let total = 0
        for (const k in weights)
            total += weights[k]
        let r = Math.random() * total
        for (const k in weights) {
            r -= weights[k]
            if (r < 0)
                return k
        }
        return "lookaround"
    }

    function playMicroIdle(name) {
        if (name === "lookaround") lookAroundAnim.start()
        else if (name === "stretch") stretchAnim.start()
        else if (name === "peek") peekAnim.start()
        else if (name === "nod") nodAnim.start()
        else if (name === "fidget") fidgetAnim.start()
    }

    // Aborts any running micro-idle and returns the axes it touched to
    // neutral. Called when a higher tier (thinking, moving, speech,
    // gesture) takes over the transform.
    function cancelMicroIdle() {
        lookAroundAnim.stop()
        stretchAnim.stop()
        peekAnim.stop()
        nodAnim.stop()
        fidgetAnim.stop()
        root.swayPaused = false
        if (!root.avatar)
            return
        root.avatar.eulerRotation.x = 0
        root.avatar.eulerRotation.z = 0
        if (!root.pulsing)
            root.avatar.scale.y = 1.0
    }

    readonly property bool microBlocked: root.avatar === null || root.thinking || root.moving
        || root.pulsing || gestureAnim.running || root.microRunning

    onThinkingChanged: if (thinking) cancelMicroIdle()

    Timer {
        id: microTimer
        running: root.avatar !== null
        repeat: true
        interval: 6000
        onTriggered: {
            interval = 6000 + Math.random() * 9000
            if (!root.microBlocked)
                root.playMicroIdle(root.pickMicroIdle())
        }
    }

    // idle_lookaround: sweep right, then left, then settle exactly where
    // the sway loop begins (-amplitude) so it resumes without a jump.
    SequentialAnimation {
        id: lookAroundAnim
        onStarted: root.swayPaused = true
        onStopped: root.swayPaused = false
        NumberAnimation { target: root.avatar; property: "eulerRotation.y"; to: 25; duration: 650; easing.type: Easing.InOutSine }
        PauseAnimation { duration: 250 }
        NumberAnimation { target: root.avatar; property: "eulerRotation.y"; to: -25; duration: 1100; easing.type: Easing.InOutSine }
        PauseAnimation { duration: 250 }
        NumberAnimation { target: root.avatar; property: "eulerRotation.y"; to: -root.activePreset.amplitude; duration: 650; easing.type: Easing.InOutSine }
    }

    // idle_stretch: a slow vertical stretch.
    SequentialAnimation {
        id: stretchAnim
        NumberAnimation { target: root.avatar; property: "scale.y"; to: 1.07; duration: 900; easing.type: Easing.InOutSine }
        PauseAnimation { duration: 300 }
        NumberAnimation { target: root.avatar; property: "scale.y"; to: 1.0; duration: 1000; easing.type: Easing.InOutSine }
    }

    // idle_peek: tilt and lean toward the viewer, hold, return.
    SequentialAnimation {
        id: peekAnim
        ParallelAnimation {
            NumberAnimation { target: root.avatar; property: "eulerRotation.x"; to: 10; duration: 350; easing.type: Easing.OutQuad }
            NumberAnimation { target: root.avatar; property: "eulerRotation.z"; to: 14; duration: 350; easing.type: Easing.OutQuad }
        }
        PauseAnimation { duration: 700 }
        ParallelAnimation {
            NumberAnimation { target: root.avatar; property: "eulerRotation.x"; to: 0; duration: 450; easing.type: Easing.InOutQuad }
            NumberAnimation { target: root.avatar; property: "eulerRotation.z"; to: 0; duration: 450; easing.type: Easing.InOutQuad }
        }
    }

    // idle_sleepy: head droops forward and slowly comes back up.
    SequentialAnimation {
        id: nodAnim
        NumberAnimation { target: root.avatar; property: "eulerRotation.x"; to: 14; duration: 1200; easing.type: Easing.InOutSine }
        PauseAnimation { duration: 400 }
        NumberAnimation { target: root.avatar; property: "eulerRotation.x"; to: 0; duration: 700; easing.type: Easing.OutBack }
    }

    // idle_fidget: quick side-to-side shuffle.
    SequentialAnimation {
        id: fidgetAnim
        loops: 2
        NumberAnimation { target: root.avatar; property: "eulerRotation.z"; to: 6; duration: 140; easing.type: Easing.InOutQuad }
        NumberAnimation { target: root.avatar; property: "eulerRotation.z"; to: -6; duration: 220; easing.type: Easing.InOutQuad }
        NumberAnimation { target: root.avatar; property: "eulerRotation.z"; to: 0; duration: 140; easing.type: Easing.InOutQuad }
        PauseAnimation { duration: 120 }
    }



    // --- idle_blink -------------------------------------------------------
    // Drives the eye meshes' `blink` morph target (see the manifest's
    // blink_shape_keys). Avatars without eye targets never blink. Runs
    // independently of the micro-idles, since it touches only the eyes.
    property real blinkAmount: 0   // 0 = open, 1 = closed
    onBlinkAmountChanged: if (root.controller) root.controller.setBlinkWeight(blinkAmount)

    SequentialAnimation {
        id: blinkAnim
        NumberAnimation { target: root; property: "blinkAmount"; to: 1; duration: 70; easing.type: Easing.OutQuad }
        PauseAnimation { duration: 40 }
        NumberAnimation { target: root; property: "blinkAmount"; to: 0; duration: 110; easing.type: Easing.InQuad }
    }

    Timer {
        id: blinkTimer
        running: root.avatar !== null && root.controller !== null
        repeat: true
        interval: 3000
        onTriggered: {
            // Mostly 2.5-6.5s apart, occasionally a quick double blink.
            interval = Math.random() < 0.2 ? 260 : 2500 + Math.random() * 4000
            // Checked each time: a hot-reloaded look replaces the nodes.
            if (!blinkAnim.running && root.controller.hasBlinkTargets())
                blinkAnim.start()
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
        cancelMicroIdle()
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
