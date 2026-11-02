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
    // screen the model turns to face its heading and hops (or, with run
    // targets, runs).
    property bool moving: false
    // Height (m) the avatar node sits at so it rotates/scales about the waist
    // (see Avatar3D.qml). position.y rests here rather than at 0.
    property real pivotHeight: 0
    onPivotHeightChanged: restoreRestHeight()
    onAvatarChanged: restoreRestHeight()
    function restoreRestHeight() {
        if (root.avatar && !hopLoop.running)
            root.avatar.position.y = root.pivotHeight
    }
    property int travelDir: 1   // +1 right, -1 left
    SequentialAnimation {
        id: hopLoop
        loops: Animation.Infinite
        running: root.avatar !== null && root.moving
        NumberAnimation { target: root.avatar; property: "position.y"; to: root.pivotHeight + 0.14; duration: 150; easing.type: Easing.OutQuad }
        NumberAnimation { target: root.avatar; property: "position.y"; to: root.pivotHeight; duration: 150; easing.type: Easing.InQuad }
        onRunningChanged: if (!running) root.restoreRestHeight()
    }
    // Facing. Before a run the avatar turns 90 degrees toward its heading
    // (running sideways-on, sliding, looks wrong), and turns back to face the
    // user once it arrives. Main.qml delays the slide by `turnMs` so the run
    // starts after the turn. +90 on Y turns the front toward screen-right.
    readonly property int turnMs: 220
    // Set by Main.qml for the whole trip between monitors, so the stop at the
    // edge of one screen does not turn the companion back before the run in.
    property bool holdTurn: false
    property bool turned: false   // true from the start of the turn-away until the turn-back finishes

    // True when it started a turn (the caller waits `turnMs` before running);
    // false when already side-on that way, as on the second leg of a trip
    // between monitors.
    function turnToward(dir) {
        if (!root.avatar)
            return false
        if (root.turned && !turnAnim.running && Math.abs(root.avatar.eulerRotation.y - 90 * dir) < 1)
            return false
        cancelMicroIdle()
        turnAnim.stop()   // before `turned = true`: its onStopped may clear it
        root.turned = true
        turnAnim.to = 90 * dir
        turnAnim.start()
        return true
    }

    // Ends exactly at the sway's start angle (-amplitude), so the sway
    // resumes without a jump.
    function turnBack() {
        if (!root.turned || !root.avatar)
            return
        turnAnim.stop()
        turnAnim.to = -root.activePreset.amplitude
        turnAnim.start()
    }

    NumberAnimation {
        id: turnAnim
        target: root.avatar
        property: "eulerRotation.y"
        duration: root.turnMs
        easing.type: Easing.InOutQuad
        onStopped: if (to === -root.activePreset.amplitude) root.turned = false
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
        duration: 600   // two hops (150ms up + 150ms down each)
        loops: Animation.Infinite
        running: root.avatar !== null && (root.moving && root.canRun || root.runAmount > 0)
    }

    // Arm gestures while she speaks, for a model rigged with its own arms: the
    // generator bakes four arm poses as morph targets (gesture_l_out/_l_in for the
    // first arm, gesture_r_out/_r_in for the other) and each arm swings between its
    // two, like the run's pair of legs. Phrases of one arm, the other or both, with
    // a pause between; not while she runs.
    property bool speaking: false
    property bool canGesture: false
    property bool gesturing: false
    property real gestEnvL: 0     // 0 = arm at rest, 1 = full swing
    property real gestEnvR: 0
    property real gestPhaseL: 0
    property real gestPhaseR: 0
    Behavior on gestEnvL { NumberAnimation { duration: 260; easing.type: Easing.InOutQuad } }
    Behavior on gestEnvR { NumberAnimation { duration: 260; easing.type: Easing.InOutQuad } }
    function applyGesturePose() {
        if (!root.controller || !root.canGesture)
            return
        const kL = 0.5 + 0.5 * Math.sin(2 * Math.PI * root.gestPhaseL)
        const kR = 0.5 + 0.5 * Math.sin(2 * Math.PI * root.gestPhaseR)
        root.controller.setGestureWeights([root.gestEnvL * kL, root.gestEnvL * (1 - kL),
                                           root.gestEnvR * kR, root.gestEnvR * (1 - kR)])
    }
    onGestEnvLChanged: applyGesturePose()
    onGestEnvRChanged: applyGesturePose()
    onGestPhaseLChanged: applyGesturePose()
    onGestPhaseRChanged: applyGesturePose()
    NumberAnimation {
        id: gestCycleL
        target: root; property: "gestPhaseL"; from: 0; to: 1; duration: 900
        loops: Animation.Infinite
        running: root.canGesture && root.gestEnvL > 0
    }
    NumberAnimation {
        id: gestCycleR
        target: root; property: "gestPhaseR"; from: 0; to: 1; duration: 1300
        loops: Animation.Infinite
        running: root.canGesture && root.gestEnvR > 0
    }
    Timer { id: gestureTimer; onTriggered: root.nextGesturePhase() }
    function endGesture() {
        gestureTimer.stop()
        root.gesturing = false
        root.gestEnvL = 0
        root.gestEnvR = 0
    }
    function nextGesturePhase() {
        if (!root.speaking) {
            nextIdleArmPhase()
            return
        }
        if (!root.canGesture || root.moving) {
            endGesture()
            return
        }
        if (root.gesturing) {   // a phrase is ending: arms settle, then a pause
            root.gesturing = false
            root.gestEnvL = 0
            root.gestEnvR = 0
            gestureTimer.interval = 350 + Math.random() * 750
        } else {
            const pick = Math.floor(Math.random() * 3)   // 0: first arm, 1: other arm, 2: both
            gestCycleL.duration = 700 + Math.random() * 500
            gestCycleR.duration = 1000 + Math.random() * 600
            root.gesturing = true
            root.gestEnvL = pick !== 1 ? 0.75 + 0.25 * Math.random() : 0
            root.gestEnvR = pick !== 0 ? 0.7 + 0.3 * Math.random() : 0
            gestureTimer.interval = 1100 + Math.random() * 1400
        }
        gestureTimer.restart()
    }
    // Idle arms: while she is just standing there the same two poses are driven
    // gently, a slow drift most of the time and now and then a bigger wave or
    // shift, scaled by mood. Anything that takes over the body (speech, running,
    // thinking, the turn) puts the arms back at rest.
    readonly property real armMood: ({ "fidget": 1.4, "curious": 1.15, "neutral": 1.0, "content": 0.85, "bored": 0.6 })[root.mood] || 1.0
    property bool armIdle: false
    readonly property bool idleArmsOk: root.avatar !== null && !root.speaking && !root.moving
        && !root.thinking && !root.turned && !root.pulsing
    onIdleArmsOkChanged: {
        if (!idleArmsOk && armIdle) {
            armIdle = false
            endGesture()
        }
    }
    Timer {
        id: idleArmStart
        interval: 2500
        running: root.idleArmsOk && !root.armIdle
        onTriggered: root.nextIdleArmPhase()
    }
    function nextIdleArmPhase() {
        if (!root.idleArmsOk) {
            root.armIdle = false
            endGesture()
            return
        }
        // Looked up here, not once: a hot-reloaded look replaces the nodes.
        root.canGesture = root.controller !== null && root.controller.hasGestureTargets()
        if (!root.canGesture) {
            root.armIdle = false
            return
        }
        root.armIdle = true
        if (Math.random() < 0.22 * root.armMood) {   // a bigger move: a wave or a shift
            const pick = Math.floor(Math.random() * 3)
            gestCycleL.duration = 800 + Math.random() * 600
            gestCycleR.duration = 1000 + Math.random() * 600
            root.gestEnvL = pick !== 1 ? 0.5 + 0.3 * Math.random() : 0
            root.gestEnvR = pick !== 0 ? 0.45 + 0.3 * Math.random() : 0
            gestureTimer.interval = 1800 + Math.random() * 1400
        } else {                                      // the slow drift
            gestCycleL.duration = 4200 + Math.random() * 2600
            gestCycleR.duration = 5200 + Math.random() * 3000
            root.gestEnvL = Math.min(0.5, (0.15 + 0.15 * Math.random()) * root.armMood)
            root.gestEnvR = Math.min(0.5, (0.12 + 0.15 * Math.random()) * root.armMood)
            gestureTimer.interval = 5000 + Math.random() * 6000
        }
        gestureTimer.restart()
    }

    onSpeakingChanged: {
        root.armIdle = false
        if (speaking) {
            root.canGesture = root.controller !== null && root.controller.hasGestureTargets()
            if (root.canGesture) {
                root.gesturing = false
                gestureTimer.interval = 150
                gestureTimer.restart()
            }
        } else {
            endGesture()
        }
    }

    onMovingChanged: {
        if (moving) {
            cancelMicroIdle()
            endGesture()
            // Checked per trip: a hot-reloaded look replaces the nodes.
            root.canRun = root.controller !== null && root.controller.hasRunTargets()
        } else if (root.speaking && root.canGesture) {
            gestureTimer.interval = 400
            gestureTimer.restart()
        }
        root.runAmount = (moving && root.canRun) ? 1 : 0
        if (!moving && !holdTurn)
            turnBack()
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
        running: root.avatar !== null && !root.thinking && !root.swayPaused && !root.turned
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
        || peekAnim.running || nodAnim.running || fidgetAnim.running || clipBusy

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

    // The baked clip standing in for each micro-idle (see the generator's
    // CORE_IDLE_CLIPS / MOOD_IDLE_CLIPS). When the loaded avatar has the
    // clip it is played instead of the transform-based stand-in below, so
    // imported models bring their own idle motion.
    readonly property var bakedIdleClips: ({
        "lookaround": "idle_lookaround",
        "stretch": "idle_stretch",
        "peek": "idle_peek",
        "nod": "idle_sleepy",
        "fidget": "idle_fidget"
    })
    // Off: played through Qt's glTF import, the generator's idle clips move
    // nothing visible on a skinned avatar (checked frame by frame), so using
    // them replaced the working transform idles below with a motionless one.
    // Turn on for models whose own clips are known to play.
    property bool preferBakedClips: false
    property bool clipBusy: false
    Timer {
        id: clipBusyTimer
        onTriggered: root.clipBusy = false
    }

    function playMicroIdle(name) {
        const clip = bakedIdleClips[name]
        if (root.preferBakedClips && clip && root.controller && root.controller.playClip(clip, 1)) {
            root.clipBusy = true
            clipBusyTimer.interval = root.controller.clipDurationMs(clip) + 100
            clipBusyTimer.restart()
            return
        }
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
        if (root.clipBusy && root.controller)
            root.controller.stopAllClips()
        root.clipBusy = false
        clipBusyTimer.stop()
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

    readonly property bool microBlocked: root.avatar === null || root.thinking || root.moving || root.turned
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
