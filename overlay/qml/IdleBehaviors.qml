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
    // True while a model with no mouth (has_face:false) is talking, gated by playback state: Main.qml
    // shows sound waves beside it (SoundWaves.qml) in place of a mouth, and the model itself keeps
    // still (its idle motions pause, as they do for a spoken reply).
    property bool soundWaves: false
    onSoundWavesChanged: {
        if (soundWaves)
            cancelMicroIdle();
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
            root.avatar.position.y = root.pivotHeight;
    }
    property int travelDir: 1   // +1 right, -1 left
    SequentialAnimation {
        id: hopLoop
        loops: Animation.Infinite
        running: root.avatar !== null && root.moving && !root.libraryMode
        NumberAnimation {
            target: root.avatar
            property: "position.y"
            to: root.pivotHeight + 0.14
            duration: 150
            easing.type: Easing.OutQuad
        }
        NumberAnimation {
            target: root.avatar
            property: "position.y"
            to: root.pivotHeight
            duration: 150
            easing.type: Easing.InQuad
        }
        onRunningChanged: if (!running)
            root.restoreRestHeight()
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
    function turnToward(dir, degrees = 90) {
        if (!root.avatar)
            return false;
        if (root.turned && !turnAnim.running && Math.abs(root.avatar.eulerRotation.y - degrees * dir) < 1)
            return false;
        cancelMicroIdle();
        turnAnim.stop();   // before `turned = true`: its onStopped may clear it
        root.turned = true;
        turnAnim.to = degrees * dir;
        turnAnim.start();
        return true;
    }

    // Ends exactly at the sway's start angle (-amplitude), so the sway
    // resumes without a jump.
    function turnBack() {
        if (!root.turned || !root.avatar)
            return;
        turnAnim.stop();
        turnAnim.to = -root.activePreset.amplitude;
        turnAnim.start();
    }

    NumberAnimation {
        id: turnAnim
        target: root.avatar
        property: "eulerRotation.y"
        duration: root.turnMs
        easing.type: Easing.InOutQuad
        onStopped: if (to === -root.activePreset.amplitude)
            root.turned = false
    }

    // Running legs. Avatars with legs carry `run_a`/`run_b` morph targets
    // (the two mirrored extremes of a stride); cross-fading them in time with
    // the hop makes the body actually run. Avatars without them keep the
    // plain hop-and-lean above.
    property bool canRun: false
    property real runAmount: 0     // 0 = standing, 1 = full stride
    property real runPhase: 0      // 0..1 across one two-step cycle
    Behavior on runAmount {
        NumberAnimation {
            duration: 140
        }
    }
    function applyRunPose() {
        if (!root.controller)
            return;
        // Extremes land at phase 0.25 / 0.75 = the top of each hop, which is
        // when a runner's stride is widest (the flight phase).
        const k = 0.5 + 0.5 * Math.sin(2 * Math.PI * root.runPhase);
        root.controller.setRunWeights(root.runAmount * k, root.runAmount * (1 - k));
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
    // first arm, gesture_r_out/_r_in for the other). Their weights are driven as
    // beats rather than a wave, because a constant-period swing between two poses
    // reads as a metronome: an arm lifts quickly and settles (a little overshoot),
    // holds with small flicks of random size and timing, then lowers slowly, and a
    // pause follows. The second arm of a two-armed phrase starts a moment later and
    // flicks on its own. Phrases of one arm, the other or both; not while she runs.
    property bool speaking: false
    property bool canGesture: false
    property bool gesturing: false
    property real gOutL: 0        // the four morph weights as driven now (0 = arm at rest)
    property real gInL: 0
    property real gOutR: 0
    property real gInR: 0
    property real phraseOut: 0.8  // where this phrase's arms are held, before the flicks
    property bool phraseL: false
    property bool phraseR: false

    function applyGesturePose() {
        if (!root.controller || !root.canGesture)
            return;
        root.controller.setGestureWeights([root.gOutL, root.gInL, root.gOutR, root.gInR]);
    }
    onGOutLChanged: applyGesturePose()
    onGInLChanged: applyGesturePose()
    onGOutRChanged: applyGesturePose()
    onGInRChanged: applyGesturePose()

    ParallelAnimation {
        id: armLAnim
        NumberAnimation {
            id: armLOut
            target: root
            property: "gOutL"
        }
        NumberAnimation {
            id: armLIn
            target: root
            property: "gInL"
        }
    }
    ParallelAnimation {
        id: armRAnim
        NumberAnimation {
            id: armROut
            target: root
            property: "gOutR"
        }
        NumberAnimation {
            id: armRIn
            target: root
            property: "gInR"
        }
    }
    // Moves one arm ("L" or "R") to the weights (out, inn) over `ms`, from wherever it is now.
    function moveArm(side, out, inn, ms, easing) {
        const group = side === "L" ? armLAnim : armRAnim;
        const outAnim = side === "L" ? armLOut : armROut;
        const inAnim = side === "L" ? armLIn : armRIn;
        outAnim.to = out;
        outAnim.duration = ms;
        outAnim.easing.type = easing;
        inAnim.to = inn;
        inAnim.duration = ms * (0.85 + 0.3 * Math.random());   // the two poses blend, not in lockstep
        inAnim.easing.type = easing;
        group.restart();
    }
    function lowerArms(slow) {
        const ms = (slow ? 650 : 420) + 250 * Math.random();
        moveArm("L", 0, 0, ms, Easing.InOutSine);
        moveArm("R", 0, 0, ms * (0.9 + 0.2 * Math.random()), Easing.InOutSine);
    }
    function clamp(v, lo, hi) {
        return Math.max(lo, Math.min(hi, v));
    }

    // One beat of a held gesture: a small move to a nearby pose.
    function flickArm(side) {
        const out = clamp(root.phraseOut + (Math.random() - 0.5) * 0.5, 0.25, 1.0);
        const inn = Math.random() < 0.45 ? 0 : Math.random() * 0.45;
        moveArm(side, out, inn, 200 + Math.random() * 260, Easing.OutCubic);
    }
    Timer {
        id: beatTimer
        onTriggered: {
            if (!root.gesturing)
                return;
            // The lead arm flicks most beats; the other, less often.
            if (root.phraseL && (!root.phraseR || Math.random() < 0.8))
                root.flickArm("L");
            if (root.phraseR && (!root.phraseL || Math.random() < 0.6))
                root.flickArm("R");
            beatTimer.interval = 280 + Math.random() * 620;
            beatTimer.restart();
        }
    }
    Timer {
        id: lagTimer   // the second arm of a phrase follows the first
        property string side: "R"
        onTriggered: if (root.gesturing)
            root.moveArm(side, root.phraseOut * (0.7 + 0.2 * Math.random()), 0.1 * Math.random(), 260 + Math.random() * 220, Easing.OutBack)
    }
    Timer {
        id: gestureTimer
        onTriggered: root.nextGesturePhase()
    }
    function endGesture() {
        gestureTimer.stop();
        beatTimer.stop();
        lagTimer.stop();
        root.gesturing = false;
        root.phraseL = false;
        root.phraseR = false;
        lowerArms(false);
    }
    function nextGesturePhase() {
        if (!root.speaking) {
            nextIdleArmPhase();
            return;
        }
        if (!root.canGesture || root.moving) {
            endGesture();
            return;
        }
        if (root.gesturing) {   // a phrase is ending: the arms settle slowly, then a pause
            root.gesturing = false;
            beatTimer.stop();
            lagTimer.stop();
            lowerArms(true);
            gestureTimer.interval = 450 + Math.random() * 1300;
        } else {
            const pick = Math.random();               // one arm, the other, or both
            const both = pick > 0.72;
            const leftLeads = both ? Math.random() < 0.5 : pick < 0.36;
            root.phraseL = both || leftLeads;
            root.phraseR = both || !leftLeads;
            root.phraseOut = 0.6 + 0.4 * Math.random();
            root.gesturing = true;
            const lead = leftLeads ? "L" : "R";
            // The lift is quick and settles with a little overshoot.
            moveArm(lead, root.phraseOut, 0.15 * Math.random(), 240 + Math.random() * 180, Easing.OutBack);
            if (both) {
                lagTimer.side = leftLeads ? "R" : "L";
                lagTimer.interval = 110 + Math.random() * 150;
                lagTimer.restart();
            }
            beatTimer.interval = 450 + Math.random() * 450;
            beatTimer.restart();
            gestureTimer.interval = 1300 + Math.random() * 1900;
        }
        gestureTimer.restart();
    }
    // Idle arms: while she is just standing there the arms drift gently to a small
    // new pose now and then, and sometimes make a bigger move (a shift, a half-wave),
    // scaled by mood. Anything that takes over the body (speech, running, thinking,
    // the turn) puts the arms back at rest.
    readonly property real armMood: ({
            "fidget": 1.4,
            "curious": 1.15,
            "neutral": 1.0,
            "content": 0.85,
            "bored": 0.6
        })[root.mood] || 1.0
    property bool armIdle: false
    readonly property bool idleArmsOk: root.avatar !== null && !root.speaking && !root.moving && !root.thinking && !root.turned && !root.soundWaves
    onIdleArmsOkChanged: {
        if (!idleArmsOk && armIdle) {
            armIdle = false;
            endGesture();
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
            root.armIdle = false;
            endGesture();
            return;
        }
        // Looked up here, not once: a hot-reloaded look replaces the nodes.
        root.canGesture = root.controller !== null && root.controller.hasGestureTargets();
        if (!root.canGesture) {
            root.armIdle = false;
            return;
        }
        root.armIdle = true;
        const big = Math.random() < 0.22 * root.armMood;   // a shift or a half-wave
        const which = Math.floor(Math.random() * 3);       // first arm, other arm, both
        for (const side of ["L", "R"]) {
            if (which === (side === "L" ? 1 : 0))
                continue;
            const mood = root.armMood;
            if (big) {
                moveArm(side, 0.4 + 0.3 * Math.random(), 0.1 * Math.random(), 600 + Math.random() * 500, Easing.InOutSine);
            } else {   // a slow drift to somewhere near rest
                moveArm(side, Math.min(0.3, (0.04 + 0.16 * Math.random()) * mood), Math.min(0.25, 0.12 * Math.random() * mood), 1800 + Math.random() * 2200, Easing.InOutSine);
            }
        }
        gestureTimer.interval = big ? 1900 + Math.random() * 1200 : 3500 + Math.random() * 5000;
        gestureTimer.restart();
        if (big)   // and settle back before the next drift
            idleSettle.restart();
    }
    Timer {
        id: idleSettle
        interval: 1700
        onTriggered: if (root.armIdle && !root.gesturing)
            root.lowerArms(true)
    }

    onSpeakingChanged: {
        root.libraryStateChanged();
        root.armIdle = false;
        if (speaking) {
            root.canGesture = root.controller !== null && root.controller.hasGestureTargets();
            if (root.canGesture) {
                root.gesturing = false;
                gestureTimer.interval = 150;
                gestureTimer.restart();
            }
        } else {
            endGesture();
        }
    }

    onMovingChanged: {
        root.libraryStateChanged();
        if (moving) {
            cancelMicroIdle();
            endGesture();
            // Checked per trip: a hot-reloaded look replaces the nodes.
            root.canRun = root.controller !== null && root.controller.hasRunTargets();
        } else if (root.speaking && root.canGesture) {
            gestureTimer.interval = 400;
            gestureTimer.restart();
        }
        root.runAmount = (moving && root.canRun) ? 1 : 0;
        if (!moving && !holdTurn)
            turnBack();
    }

    readonly property var moodPresets: ({
            "neutral": {
                amplitude: 4,
                periodMs: 4000
            },
            "bored": {
                amplitude: 2,
                periodMs: 6000
            },
            "curious": {
                amplitude: 6,
                periodMs: 2800
            },
            "fidget": {
                amplitude: 8,
                periodMs: 1600
            },
            "content": {
                amplitude: 3,
                periodMs: 4500
            }
        })

    readonly property var activePreset: moodPresets[mood] || moodPresets["neutral"]

    // Base idle loop + mood idle, collapsed into one continuous sway whose
    // amplitude/speed is selected by `mood` -- visually equivalent to
    // switching between separately-named idle clips.
    SequentialAnimation {
        id: idleLoop
        loops: Animation.Infinite
        running: root.avatar !== null && !root.thinking && !root.swayPaused && !root.turned && !root.libraryMode
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

    // --- Animation library ------------------------------------------------
    // A body that wears an animation library (the manifest's `library_clips`, role ->
    // clip) is animated by those clips rather than by the transform stand-ins below:
    // the idle loop whenever nothing else is going on, the talking loop while it
    // speaks, a run while it crosses the screen, and now and then a different idle or a
    // nod. One clip plays at a time, so each change of state is a change of clip.
    property var libraryClips: ({})
    readonly property bool libraryMode: root.avatar !== null && root.controller !== null && root.libraryClips !== undefined && root.libraryClips.idle !== undefined
    property string libraryPlaying: ""
    property string libraryOneShot: ""   // a clip that plays once, then the loop resumes

    // A role may have several clips to choose from: its own and `<role>.alt1`, `.alt2`... The one
    // played is picked at random, and kept for as long as the thing it is for lasts (one reply, one
    // sleep). `.mirror` is the same clip with the other hand, played half the time.
    function optionKeys(role) {
        const c = root.libraryClips;
        const keys = c[role] !== undefined ? [role] : [];
        for (let n = 1; c[role + ".alt" + n] !== undefined; ++n)
            keys.push(role + ".alt" + n);
        return keys;
    }
    function pickOption(role) {
        const keys = root.optionKeys(role);
        return keys.length ? keys[Math.floor(Math.random() * keys.length)] : "";
    }
    function mirrored(key) {
        return root.libraryClips[key + ".mirror"] !== undefined && Math.random() < 0.5 ? key + ".mirror" : key;
    }
    property string talkKey: ""
    property string sleepKey: ""
    // A way of sleeping, or of sitting on the floor, is a set of clips (lie down / sleep / shift /
    // get up; enter / idle / exit) that begin and end in the poses the next needs: the manifest
    // lists the sets as option N of each role, so the suffix of the option picked ("", ".alt1"...)
    // says which clips go with it.
    property string sleepVariant: ""
    property string floorVariant: ""
    // sitting is on a chair or on the floor: the roles of the kind chosen
    property string sitKind: ""      // "", "chair", "floor"
    function sitRole(base) {
        return root.sitKind === "floor" ? "floor_" + base + root.floorVariant : base;
    }
    // On a chair, with a clip for talking from it, it does not stand up to talk or to think about
    // what to say: it talks sitting down, and stands up when it has finished (a move still has it
    // stand first). `talkedSeated` remembers that it talked, so that it gets up afterwards.
    readonly property bool seatedTalk: root.sitKind === "chair" && root.libraryClips.sit_talk !== undefined
    property bool talkedSeated: false
    function staysSeated() {
        return root.sitState === "sitting" && root.seatedTalk && !root.moving && !root.standRequested;
    }

    function libraryWanted() {
        const c = root.libraryClips;
        if (root.sleepState === "asleep")
            return c[root.sleepKey] || c.sleep;
        if (root.sitState === "sitting") {
            if (root.speaking && root.seatedTalk)   // it talks to you from the chair
                return c.sit_talk;
            return c[root.sitRole("sit_idle")];
        }
        if (root.moving)
            return c.run || c.walk || c.idle;
        if (root.speaking) {
            if (!root.talkKey || c[root.talkKey] === undefined)
                root.talkKey = root.pickOption("talk");
            return c[root.talkKey] || c.idle;
        }
        root.talkKey = "";
        if (root.mood === "sad" && c.idle_sad)   // low: it droops
            return c.idle_sad;
        return c.idle;
    }
    // True from a model loading until its animations have settled and its first clip is started.
    property bool loading: false
    // Makes sure the clip for the current state is the one playing (restarting it if a
    // reload or reset stopped it).
    function syncLibrary() {
        if (!root.libraryMode || root.libraryOneShot !== "" || root.loading)
            return;
        // A rebuilt scene (the window moved to another output) starts with the chair showing.
        if (root.propAmount === 0)
            root.applyProp();
        const want = root.libraryWanted();
        if (want && (want !== root.libraryPlaying || !root.controller.clipRunning(want)))
            root.libraryPlaying = root.controller.playClip(want, -1) ? want : "";
    }
    // A freshly loaded body stands in its bind pose until its first clip is evaluated, so
    // Main.qml keeps it hidden until this says a clip is playing. The clip cannot start until the
    // model's animations exist, which on a big model is some time after the load reports success, so
    // it is tried again every 100 ms; the safety cap only keeps the avatar from staying hidden.
    signal revealRequested
    property int loadedCount: -1
    property int loadedStable: 0
    function modelLoaded() {
        root.loadedCount = -1;
        root.loadedStable = 0;
        root.libraryPlaying = "";   // a new model: nothing is playing on it yet
        // A one-shot that was playing on the model it replaces is not playing on this one: left
        // running, the new body would be shown standing in its bind pose until the timer ended it.
        root.loading = true;
        root.lateChecks = 0;
        root.lateCount = 0;
        root.lateRestarts = 0;
        lateWatch.restart();
        if (root.libraryOneShot !== "")
            root.endLibraryOneShot();
        revealFallback.restart();
        loadRetry.restart();
    }
    // Called every 100 ms after a load. The clip is started once, when the number of animations it
    // has has stopped growing for three checks: starting it earlier leaves the late ones (the
    // left arm, on one model) standing in the bind pose until the clip next restarts.
    function settleLoaded() {
        if (!root.libraryMode) {
            loadRetry.stop();
            root.loading = false;
            revealSoon.restart();
            return;
        }
        const want = root.libraryWanted();
        const count = want ? root.controller.clipAnimationCount(want) : 0;
        root.loadedStable = (count > 0 && count === root.loadedCount) ? root.loadedStable + 1 : 0;
        root.loadedCount = count;
        if (root.loadedStable >= 3) {
            root.loading = false;
            root.syncLibrary();
            if (root.libraryPlaying !== "") {
                loadRetry.stop();
                revealSoon.restart();
            }
        }
    }
    // A few frames for the clip to take hold of the bones.
    Timer {
        id: revealSoon
        interval: 300
        onTriggered: {
            root.syncLibrary();   // an animation that came after the clip started (all must run) is started while still hidden
            root.revealRequested();
        }
    }
    // Some animations of a clip are made after the others (the left arm's, on one model), more than
    // the 300 ms the count was stable for, and the loader's own reset also stops clips for a while
    // after a load: a body left with an arm in the bind pose. For ten seconds after a load the clip
    // is checked every 200 ms: when it has more animations than before, or has stopped (at most
    // three times: an animation that can never run must not restart it every 200 ms), syncLibrary
    // starts it again.
    property int lateChecks: 0
    property int lateCount: 0
    property int lateRestarts: 0
    Timer {
        id: lateWatch
        interval: 200
        repeat: true
        onTriggered: {
            if (++root.lateChecks > 50) {
                lateWatch.stop();
                return;
            }
            if (root.loading || root.libraryOneShot !== "")
                return;
            const want = root.libraryWanted();
            const count = want ? root.controller.clipAnimationCount(want) : 0;
            const grew = count !== root.lateCount;
            root.lateCount = count;
            const stopped = want && !root.controller.clipRunning(want) && root.lateRestarts < 3;
            if (grew || stopped) {
                if (stopped && !grew)
                    ++root.lateRestarts;
                root.syncLibrary();
            }
        }
    }
    Timer {
        id: loadRetry
        interval: 100
        repeat: true
        onTriggered: root.settleLoaded()
    }
    Timer {
        id: revealFallback
        interval: 8000
        onTriggered: {
            loadRetry.stop();
            root.loading = false;
            root.syncLibrary();
            root.revealRequested();
        }
    }
    function endLibraryOneShot() {
        const was = root.sitState;
        const slept = root.sleepState;
        root.libraryOneShot = "";
        libraryOneShotTimer.stop();
        if (root.workoutActive) {   // up on its feet again
            root.workoutActive = false;
            root.nextWorkout = root.workoutAt();
            root.turnBack();
        }
        if (slept === "lying") {
            root.sleepState = "asleep";
            root.nextSleepVariation = Date.now() + root.sleepShiftDelay(true);
            root.wakesAt = Date.now() + (root.testSleepMs > 0 ? 3 * root.testSleepMs : (45 + Math.random() * 75) * 60000);
            if (root.isBusy() || root.wakeRequested) {   // something started while it was lying down: straight back up
                root.wake();
                return;
            }
        } else if (slept === "waking") {
            root.sleepState = "";
            root.wakeRequested = false;
            root.lastUserActivity = Date.now();
            root.nextSleepAt = root.sleepAfterMs();
        } else if (slept === "asleep" && (root.wakeRequested || root.isBusy())) {
            root.wake();   // asked to get up while a variation played
            return;
        }
        if (was === "entering") {
            root.sitState = "sitting";
            root.sitUntil = Date.now() + (root.testSitMs > 0 ? root.testSitMs : (60 + Math.random() * 180) * 1000);   // sits for 1-4 minutes
            root.nextSitVariation = Date.now() + (30 + Math.random() * 60) * 1000;
            if (root.standRequested || (root.isBusy() && !root.staysSeated())) {   // something started while it was sitting down: straight back up
                root.standUp();
                return;
            }
        } else if (was === "exiting") {
            root.sitState = "";
            root.sitKind = "";
            root.talkedSeated = false;
            root.standRequested = false;
            root.noteActivity();
        } else if (was === "sitting" && (root.standRequested || (root.isBusy() && !root.staysSeated()))) {
            root.standUp();   // asked to stand while a seated variation played
            return;
        }
        syncLibrary();
    }
    function playLibraryClip(clip) {
        if (!clip || !root.controller.playClip(clip, 1, true))
            return false;
        root.libraryOneShot = clip;
        root.libraryPlaying = "";
        const ms = root.controller.clipDurationMs(clip);
        root.sitClipEndsAt = Date.now() + ms + 80;
        libraryOneShotTimer.interval = ms + 80;
        libraryOneShotTimer.restart();
        return true;
    }
    function playLibraryOneShot(role) {
        const key = root.pickOption(role);
        return key !== "" && playLibraryClip(root.libraryClips[root.mirrored(key)]);
    }

    // Gestures the brain asks for (`play_gesture`) that have a clip of their own, by role. The
    // clip played is the variant that goes with how the companion feels: `intensity` is 0..1
    // from its mood (0.5 at rest); a role converted with variants has `.low` and `.high` clips.
    readonly property var libraryGestures: ({
            greet: "greet",
            celebrate: "celebrate",
            angry: "angry",
            hip_thrust: "hip_thrust"
        })
    function libraryGestureClip(name, intensity) {
        const role = root.libraryGestures[name];
        if (!role)
            return "";
        const c = root.libraryClips;
        let key = root.pickOption(role);   // one of the ways to do it
        if (key === "")
            return "";
        if (intensity >= 0.65 && c[key + ".high"])
            key += ".high";
        else if (intensity <= 0.35 && c[key + ".low"])
            key += ".low";
        return c[root.mirrored(key)] || c[key] || "";
    }
    Timer {
        id: libraryOneShotTimer
        onTriggered: root.endLibraryOneShot()
    }

    // --- Sitting down ---------------------------------------------------------
    // After a long idle it may sit on the ground: the enter clip, then the sit idle loop for a
    // while (nothing else plays), then the exit clip, which also plays before anything that
    // has it interact with the user (speaking, moving, thinking) so it never talks from the
    // floor. Needs all three clips (the manifest's sit_enter / sit_idle / sit_exit).
    readonly property bool chairSet: root.libraryClips.sit_enter !== undefined && root.libraryClips.sit_idle !== undefined && root.libraryClips.sit_exit !== undefined
    readonly property bool floorSet: root.libraryClips.floor_sit_enter !== undefined && root.libraryClips.floor_sit_idle !== undefined && root.libraryClips.floor_sit_exit !== undefined
    readonly property bool canSit: root.libraryMode && (root.chairSet || root.floorSet)
    property string sitState: ""      // "", "entering", "sitting", "exiting"
    // A function, not a binding: the handlers that call it run before a binding on the same
    // properties has been re-evaluated.
    function isBusy() {
        return root.speaking || root.moving || root.thinking;
    }
    property double lastActivity: Date.now()
    // DESKMATE_SIT_TEST_SECONDS (testing): sit after that long idle, for that long, checked every 2 s.
    // The test* seconds are context properties from main.cpp (absent unless set), which qmllint cannot see.
    // qmllint disable unqualified
    readonly property int testSitMs: typeof testSitSeconds === "number" ? testSitSeconds * 1000 : 0
    // qmllint enable unqualified
    function nextSitAfter() {
        return root.testSitMs > 0 ? root.testSitMs : (4 + Math.random() * 8) * 60000;
    }
    property double sitAfter: nextSitAfter()    // idle for 4-12 minutes first
    property double sitUntil: 0
    property double nextSitVariation: 0   // now and then, while it sits, a different seated clip
    property int sitCheckMs: root.testSitMs > 0 ? 2000 : 10000

    // The chair under a sitting body (the manifest's sit_prop) is shown from partway through
    // the sit-down (just behind where it stands) to partway through the stand-up.
    property bool propShown: false
    property real propAmount: propShown ? 1 : 0   // it grows in and shrinks away, not pops
    Behavior on propAmount {
        NumberAnimation {
            duration: 300
            easing.type: Easing.OutQuad
        }
    }
    function applyProp() {
        if (root.libraryClips.sit_prop && root.controller)
            root.controller.setSitPropShown(root.propAmount);
    }
    onPropAmountChanged: applyProp()
    Timer {
        id: propShowTimer
        onTriggered: root.propShown = true
    }
    Timer {
        id: propHideTimer
        onTriggered: root.propShown = false
    }

    // The body is to move: asks it to get up first (and, if it is still sitting down, to do
    // that straight afterwards) and returns how many ms until it is standing with the chair
    // gone, 0 when it is not sitting. The caller holds the turn and the run back that long, so
    // it does not turn or run while still sitting on the chair.
    property bool standRequested: false
    property double sitClipEndsAt: 0
    function sitExitMs() {
        const exit = root.libraryClips[root.sitRole("sit_exit")];
        return exit ? root.controller.clipDurationMs(exit) + 80 : 0;
    }
    function requestStand() {
        if (root.sleepState !== "" || root.workoutActive) {   // it is on the floor: up first, and the move waits
            const left = Math.max(0, root.sitClipEndsAt - Date.now());
            const wakeClip = root.libraryClips[root.wakeKey()];
            const wakeMs = wakeClip ? root.controller.clipDurationMs(wakeClip) + 80 : 0;
            if (root.workoutActive)
                return left;
            root.wakeRequested = true;
            if (root.sleepState === "asleep") {
                const variationPlaying = root.libraryOneShot !== "";   // it gets up when that ends
                root.wake();
                return Math.max(0, root.sitClipEndsAt - Date.now()) + (variationPlaying ? wakeMs : 0);
            }
            return root.sleepState === "lying" ? left + wakeMs : left;
        }
        if (!root.canSit || root.sitState === "")
            return 0;
        root.standRequested = true;
        const left = Math.max(0, root.sitClipEndsAt - Date.now());
        if (root.sitState === "sitting") {
            root.standUp();   // (it waits for a seated variation in progress)
            return left + root.sitExitMs();
        }
        return root.sitState === "entering" ? left + root.sitExitMs() : left;
    }

    function noteActivity() {
        root.lastActivity = Date.now();
        root.sitAfter = root.nextSitAfter();
    }
    function playSitClip(role, state) {
        const clip = root.libraryClips[role];
        if (!clip || !root.controller.playClip(clip, 1, true))
            return false;
        root.sitState = state;
        root.libraryOneShot = clip;
        root.libraryPlaying = "";
        const ms = root.controller.clipDurationMs(clip);
        root.sitClipEndsAt = Date.now() + ms + 80;
        libraryOneShotTimer.interval = ms + 80;
        libraryOneShotTimer.restart();
        if (root.sitKind !== "chair") {   // on the floor there is no chair
            // nothing to show
        } else if (state === "entering") {   // the chair appears as it sits down, and goes as it stands up
            propHideTimer.stop();
            propShowTimer.interval = ms * 0.1;
            propShowTimer.restart();
        } else if (state === "exiting") {
            propShowTimer.stop();
            propHideTimer.interval = ms * 0.5;
            propHideTimer.restart();
        }
        return true;
    }
    function sitDown() {
        if (!(root.canSit && root.sitState === "" && root.sleepState === "" && !root.workoutActive && !root.isBusy() && root.libraryOneShot === ""))
            return;
        const kinds = (root.chairSet ? ["chair"] : []).concat(root.floorSet ? ["floor"] : []);
        root.sitKind = kinds[Math.floor(Math.random() * kinds.length)];   // a chair or the floor, as it feels
        root.floorVariant = root.sitKind === "floor" ? root.pickOption("floor_sit_idle").substring("floor_sit_idle".length) : "";
        if (!playSitClip(root.sitRole("sit_enter"), "entering"))
            root.sitKind = "";
    }
    function standUp() {
        if (root.libraryOneShot !== "")   // a seated variation is playing: it stands up when that ends
            return;
        if (root.sitState === "sitting" && !playSitClip(root.sitRole("sit_exit"), "exiting")) {
            root.sitState = "";   // no clip to play: carry on from wherever it is
            syncLibrary();
        }
    }

    // --- Sleeping and working out ---------------------------------------------
    // Gone for a long time (nothing said, no move, no thinking for 30-60 minutes) it lies down
    // (`lie_down`: the stand-up played backwards, eased into the sleeping pose), sleeps (the
    // `sleep` loop, with `sleep_alt` now and then) and, when anything starts, gets up first
    // (`wake`, which ends standing like the idle) before it speaks, moves or thinks. From the
    // chair it stands up first. Now and then, standing idle, it works out (`workout`,
    // `workout_floor`: each lies down, does the exercise and stands again, in one clip, which
    // nothing interrupts).
    readonly property bool canSleep: root.libraryMode && root.libraryClips.lie_down !== undefined && root.libraryClips.sleep !== undefined && root.libraryClips.wake !== undefined
    property string sleepState: ""   // "", "lying", "asleep", "waking"
    property bool wakeRequested: false
    property bool workoutActive: false
    readonly property bool floorBusy: root.sleepState !== "" || root.workoutActive
    // DESKMATE_SLEEP_TEST_SECONDS / DESKMATE_WORKOUT_TEST_SECONDS (testing): sleep after that long
    // idle, work out after that long; checked every 2 s.
    // qmllint disable unqualified
    readonly property int testSleepMs: typeof testSleepSeconds === "number" ? testSleepSeconds * 1000 : 0
    readonly property int testWorkoutMs: typeof testWorkoutSeconds === "number" ? testWorkoutSeconds * 1000 : 0
    // qmllint enable unqualified
    // DESKMATE_ATTENTION_TEST_SECONDS (testing): ask for attention every that many seconds.
    // qmllint disable unqualified
    readonly property int testAttentionMs: typeof testAttentionSeconds === "number" ? testAttentionSeconds * 1000 : 0
    // qmllint enable unqualified
    function sleepAfterMs() {
        return root.testSleepMs > 0 ? root.testSleepMs : (30 + Math.random() * 30) * 60000;
    }
    function workoutAt() {
        return Date.now() + (root.testWorkoutMs > 0 ? root.testWorkoutMs : (10 + Math.random() * 20) * 60000);
    }
    property double lastUserActivity: Date.now()
    property double nextSleepAt: sleepAfterMs()
    property double nextWorkout: workoutAt()
    property double nextSleepVariation: 0
    // How long it lies breathing before it shifts in its sleep (`sleep_alt`: a slow turn and back,
    // 20 s): 2.5-5 minutes the first time, then 3-8 minutes between shifts. When testing, N * 1.2 s.
    function sleepShiftDelay(first) {
        if (root.testSleepMs > 0)
            return root.testSleepMs * 1.2;
        return (first ? 150 + Math.random() * 150 : 180 + Math.random() * 300) * 1000;
    }
    property double wakesAt: 0   // it sleeps for an hour or so, and then gets up by itself
    // The clip of the way of sleeping in use for `base` (its own, else the first way's).
    function sleepClipKey(base) {
        return root.libraryClips[base + root.sleepVariant] !== undefined ? base + root.sleepVariant : base;
    }
    function wakeKey() {
        return root.sleepClipKey("wake");
    }
    function playSleepClip(key, state) {
        if (!playLibraryClip(root.libraryClips[key]))
            return false;
        root.sleepState = state;
        return true;
    }
    function fallAsleep() {
        if (root.canSleep && root.sleepState === "" && root.sitState === "" && !root.isBusy() && root.libraryOneShot === "") {
            root.sleepKey = root.pickOption("sleep");   // one of the ways to sleep, for this sleep
            root.sleepVariant = root.sleepKey.substring("sleep".length);
            playSleepClip(root.sleepClipKey("lie_down"), "lying");
        }
    }
    function wake() {
        if (root.libraryOneShot !== "")   // lying down or a variation is playing: it gets up when that ends
            return;
        // Called to be up and about: the quick stand-up. Left to wake itself, the slow one.
        const slow = !root.isBusy() && !root.wakeRequested && root.libraryClips["wake_slow" + root.sleepVariant] !== undefined;
        if (root.sleepState === "asleep" && !playSleepClip(slow ? "wake_slow" + root.sleepVariant : root.wakeKey(), "waking")) {
            root.sleepState = "";   // no clip to play: carry on from wherever it is
            syncLibrary();
        }
    }
    function startWorkout() {
        if (!root.libraryMode || root.sleepState !== "" || root.sitState !== "" || root.isBusy() || root.libraryOneShot !== "")
            return;
        const keys = root.optionKeys("workout").concat(root.optionKeys("workout_floor"));   // every way it has to work out
        if (!keys.length)
            return;
        const key = keys[Math.floor(Math.random() * keys.length)];
        const clip = root.libraryClips[key];
        // A workout lies along the way the body faces, toward the camera, and a body that long and that
        // near grows to half as big again. Every workout (the one clip is the lying down, the exercise
        // and the getting up) is done turned 45 degrees: half the growth, and still facing the user
        // enough for the ones that look best that way (a burpee, jumping jacks). It turns back when up.
        if (root.turnToward(1, root.workoutTurnDegrees)) {
            root.workoutActive = true;
            root.workoutClip = clip;
            cancelMicroIdle();
            workoutDelay.restart();
        } else if (playLibraryClip(clip)) {
            root.workoutActive = true;
            cancelMicroIdle();
        }
    }
    property string workoutClip: ""
    readonly property int workoutTurnDegrees: 45
    Timer {
        id: workoutDelay
        interval: root.turnMs + 40
        onTriggered: {
            if (!root.playLibraryClip(root.workoutClip)) {   // nothing to play after all: up and facing the user
                root.workoutActive = false;
                root.turnBack();
            }
        }
    }
    // --- Asking for attention, and thinking hard --------------------------------
    // Left alone standing for a while it tries to get the user's attention (`attention`: a raised
    // arm, both arms up), once after 3-8 minutes without anything said, moved or thought, and
    // then now and then (10-25 minutes) for as long as it is ignored; anything that happens
    // starts the wait again. When it has been thinking for a long time (6 s, and again every
    // 20 s) it plays `think_long` (a hand to the head: it does not know the answer); the reply
    // that ends the thought cuts it short.
    property double nextAttention: attentionDelay(true)
    function attentionDelay(first) {
        if (root.testAttentionMs > 0)
            return Date.now() + root.testAttentionMs;
        return Date.now() + (first ? 180 + Math.random() * 300 : 600 + Math.random() * 900) * 1000;
    }
    property int thoughtMs: 6000
    Timer {
        id: deepThoughtTimer
        interval: root.thoughtMs
        repeat: true
        running: root.libraryMode && root.thinking && root.libraryClips.think_long !== undefined
        onTriggered: {
            if (root.sitState === "" && root.sleepState === "" && !root.workoutActive && !root.speaking && !root.moving && root.libraryOneShot === "") {
                root.thoughtMs = 20000;
                root.playLibraryOneShot("think_long");
            }
        }
    }
    Timer {
        interval: root.testSleepMs > 0 || root.testWorkoutMs > 0 || root.testAttentionMs > 0 ? 2000 : 10000
        repeat: true
        running: root.libraryMode
        onTriggered: {
            if (root.sleepState === "asleep") {
                if (root.libraryOneShot === "" && Date.now() >= root.wakesAt && !root.isBusy()) {
                    root.wake();
                    return;
                }
                const shift = root.libraryClips["sleep_alt" + root.sleepVariant];   // only for the way of sleeping it belongs to
                if (root.libraryOneShot === "" && shift && Date.now() >= root.nextSleepVariation && !root.isBusy()) {
                    root.nextSleepVariation = Date.now() + root.sleepShiftDelay(false);
                    root.playLibraryClip(shift);
                }
                return;
            }
            if (root.sleepState !== "" || root.workoutActive || root.isBusy() || root.libraryOneShot !== "")
                return;
            const idleFor = Date.now() - root.lastUserActivity;
            if (root.canSleep && idleFor > root.nextSleepAt) {
                if (root.sitState === "sitting")
                    root.standUp();
                else
                    // gets up from the chair first; it lies down next time round
                    root.fallAsleep();
            } else if (root.sitState === "" && Date.now() >= root.nextWorkout) {
                root.nextWorkout = root.workoutAt();
                root.startWorkout();
            } else if (root.sitState === "" && root.libraryClips.attention !== undefined && Date.now() >= root.nextAttention) {
                root.nextAttention = root.attentionDelay(false);
                root.playLibraryOneShot("attention");
            }
        }
    }

    // Speech, a move or thinking started or ended.
    function libraryStateChanged() {
        if (!root.libraryMode)
            return;
        root.noteActivity();
        root.lastUserActivity = Date.now();
        root.nextSleepAt = root.sleepAfterMs();
        root.nextAttention = root.attentionDelay(true);
        if (!root.thinking)
            root.thoughtMs = 6000;
        if (root.sleepState === "asleep" && root.isBusy()) {
            wake();
            return;
        }
        if (root.sleepState !== "" || root.workoutActive)
            // it finishes first, and then looks at what is going on
            return;
        if (root.staysSeated() && root.sitState === "sitting") {
            if (root.speaking)
                root.talkedSeated = true;
            else if (root.talkedSeated && !root.thinking) {   // it has said what it had to say: up
                root.talkedSeated = false;
                if (root.libraryOneShot === "") {
                    standUp();
                    return;
                }
            }
            if (root.libraryOneShot !== "" && root.speaking) {   // a seated variation is playing: it stops for the talk
                root.libraryOneShot = "";
                libraryOneShotTimer.stop();
            }
            syncLibrary();
            return;
        }
        if (root.sitState === "sitting" && root.isBusy()) {
            standUp();
            return;
        }
        if (root.sitState === "entering" || root.sitState === "exiting")
            // it finishes first, and then looks at what is going on
            return;
        root.libraryOneShot = "";
        libraryOneShotTimer.stop();
        syncLibrary();
    }
    Timer {
        interval: root.sitCheckMs
        repeat: true
        running: root.canSit
        onTriggered: {
            if (root.sitState === "sitting") {
                if (Date.now() >= root.sitUntil && !root.isBusy())
                    root.standUp();
                else if (root.sitKind === "chair" && Date.now() >= root.nextSitVariation && root.libraryOneShot === "" && !root.isBusy()) {
                    root.nextSitVariation = Date.now() + (40 + Math.random() * 80) * 1000;
                    root.playLibraryOneShot("sit_alt");
                }
            } else if (root.sitState === "" && !root.isBusy() && root.libraryOneShot === "" && Date.now() - root.lastActivity > root.sitAfter) {
                root.sitDown();
            }
        }
    }
    // A hot reload or a reset can stop the clip from underneath it: look again now and then.
    Timer {
        interval: 700
        repeat: true
        running: root.libraryMode
        onTriggered: {
            root.syncLibrary();
            root.applyProp();   // a reload makes new nodes, which start out visible
        }
    }
    onLibraryModeChanged: {
        root.libraryPlaying = "";
        root.libraryOneShot = "";
        root.sitState = "";
        root.sitKind = "";
        root.talkedSeated = false;
        root.sleepState = "";
        root.sleepVariant = "";
        root.floorVariant = "";
        root.wakeRequested = false;
        root.workoutActive = false;
        root.standRequested = false;
        propShowTimer.stop();
        propHideTimer.stop();
        root.propShown = false;
        root.applyProp();
        if (libraryMode)
            syncLibrary();
    }
    // --- Tier 1/2 idle variety ------------------------------------------
    // Every so often the avatar performs one short, randomly chosen idle
    // (look around, stretch, peek, sleepy nod, fidget) on top of the
    // continuous sway so it doesn't look like a single looping motion. Which
    // ones are likely depends on `mood`. These are transform-based stand-ins
    // for the baked idle_* clips, which the overlay never plays.
    property bool swayPaused: false   // look-around owns eulerRotation.y while it runs

    readonly property var microWeights: ({
            "neutral": {
                lookaround: 3,
                stretch: 2,
                peek: 2,
                nod: 1,
                fidget: 1
            },
            "bored": {
                lookaround: 3,
                stretch: 3,
                peek: 1,
                nod: 3,
                fidget: 1
            },
            "curious": {
                lookaround: 4,
                stretch: 1,
                peek: 4,
                nod: 0,
                fidget: 1
            },
            "fidget": {
                lookaround: 2,
                stretch: 1,
                peek: 1,
                nod: 0,
                fidget: 5
            },
            "content": {
                lookaround: 2,
                stretch: 3,
                peek: 2,
                nod: 2,
                fidget: 0
            }
        })

    readonly property bool microRunning: lookAroundAnim.running || stretchAnim.running || peekAnim.running || nodAnim.running || fidgetAnim.running || clipBusy

    function pickMicroIdle() {
        const weights = microWeights[mood] || microWeights["neutral"];
        let total = 0;
        for (const k in weights)
            total += weights[k];
        let r = Math.random() * total;
        for (const k in weights) {
            r -= weights[k];
            if (r < 0)
                return k;
        }
        return "lookaround";
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
    // Off by default: the generator's own idle clips drive bones named root/spine/head,
    // and on a skinned avatar built that way they move nothing visible (checked frame by
    // frame), so using them replaced the working transform idles below with a motionless
    // one. Main.qml turns it on for a model whose manifest entry says `skeletal_clips`:
    // an imported rig whose head/neck/spine clips were baked on its own bones, which move.
    property bool preferBakedClips: false
    property bool clipBusy: false
    Timer {
        id: clipBusyTimer
        onTriggered: root.clipBusy = false
    }

    function playMicroIdle(name) {
        if (root.libraryMode) {
            // The folded-arms idle only when bored: crossed arms read as impatient or upset, so
            // they would be wrong at any other mood. Otherwise the idle loop just carries on.
            if (root.mood === "bored") {
                if (!playLibraryOneShot("idle_alt"))
                    syncLibrary();
            } else if ((name === "lookaround" || name === "peek") && playLibraryOneShot("idle_look")) {
                // it looks about
                return;
            } else {
                syncLibrary();
            }
            return;
        }
        const clip = bakedIdleClips[name];
        if (root.preferBakedClips && clip && root.controller && root.controller.playClip(clip, 1)) {
            root.clipBusy = true;
            clipBusyTimer.interval = root.controller.clipDurationMs(clip) + 100;
            clipBusyTimer.restart();
            return;
        }
        if (name === "lookaround")
            lookAroundAnim.start();
        else if (name === "stretch")
            stretchAnim.start();
        else if (name === "peek")
            peekAnim.start();
        else if (name === "nod")
            nodAnim.start();
        else if (name === "fidget")
            fidgetAnim.start();
    }

    // Aborts any running micro-idle and returns the axes it touched to
    // neutral. Called when a higher tier (thinking, moving, speech,
    // gesture) takes over the transform.
    function cancelMicroIdle() {
        if (root.libraryOneShot !== "" && root.sitState === "" && !root.floorBusy)
            root.endLibraryOneShot();
        if (root.clipBusy && root.controller)
            root.controller.stopAllClips();
        root.clipBusy = false;
        clipBusyTimer.stop();
        lookAroundAnim.stop();
        stretchAnim.stop();
        peekAnim.stop();
        nodAnim.stop();
        fidgetAnim.stop();
        root.swayPaused = false;
        if (!root.avatar)
            return;
        root.avatar.eulerRotation.x = 0;
        root.avatar.eulerRotation.z = 0;
        if (!root.soundWaves)
            root.avatar.scale.y = 1.0;
    }

    readonly property bool microBlocked: root.avatar === null || root.thinking || root.moving || root.turned || root.soundWaves || gestureAnim.running || root.microRunning || root.libraryOneShot !== "" || root.sitState !== "" || root.floorBusy

    onThinkingChanged: {
        root.libraryStateChanged();
        if (thinking)
            cancelMicroIdle();
    }

    Timer {
        id: microTimer
        running: root.avatar !== null
        repeat: true
        interval: 6000
        onTriggered: {
            interval = 6000 + Math.random() * 9000;
            if (!root.microBlocked)
                root.playMicroIdle(root.pickMicroIdle());
        }
    }

    // idle_lookaround: sweep right, then left, then settle exactly where
    // the sway loop begins (-amplitude) so it resumes without a jump.
    SequentialAnimation {
        id: lookAroundAnim
        onStarted: root.swayPaused = true
        onStopped: root.swayPaused = false
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.y"
            to: 25
            duration: 650
            easing.type: Easing.InOutSine
        }
        PauseAnimation {
            duration: 250
        }
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.y"
            to: -25
            duration: 1100
            easing.type: Easing.InOutSine
        }
        PauseAnimation {
            duration: 250
        }
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.y"
            to: -root.activePreset.amplitude
            duration: 650
            easing.type: Easing.InOutSine
        }
    }

    // idle_stretch: a slow vertical stretch.
    SequentialAnimation {
        id: stretchAnim
        NumberAnimation {
            target: root.avatar
            property: "scale.y"
            to: 1.07
            duration: 900
            easing.type: Easing.InOutSine
        }
        PauseAnimation {
            duration: 300
        }
        NumberAnimation {
            target: root.avatar
            property: "scale.y"
            to: 1.0
            duration: 1000
            easing.type: Easing.InOutSine
        }
    }

    // idle_peek: tilt and lean toward the viewer, hold, return.
    SequentialAnimation {
        id: peekAnim
        ParallelAnimation {
            NumberAnimation {
                target: root.avatar
                property: "eulerRotation.x"
                to: 10
                duration: 350
                easing.type: Easing.OutQuad
            }
            NumberAnimation {
                target: root.avatar
                property: "eulerRotation.z"
                to: 14
                duration: 350
                easing.type: Easing.OutQuad
            }
        }
        PauseAnimation {
            duration: 700
        }
        ParallelAnimation {
            NumberAnimation {
                target: root.avatar
                property: "eulerRotation.x"
                to: 0
                duration: 450
                easing.type: Easing.InOutQuad
            }
            NumberAnimation {
                target: root.avatar
                property: "eulerRotation.z"
                to: 0
                duration: 450
                easing.type: Easing.InOutQuad
            }
        }
    }

    // idle_sleepy: head droops forward and slowly comes back up.
    SequentialAnimation {
        id: nodAnim
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.x"
            to: 14
            duration: 1200
            easing.type: Easing.InOutSine
        }
        PauseAnimation {
            duration: 400
        }
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.x"
            to: 0
            duration: 700
            easing.type: Easing.OutBack
        }
    }

    // idle_fidget: quick side-to-side shuffle.
    SequentialAnimation {
        id: fidgetAnim
        loops: 2
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.z"
            to: 6
            duration: 140
            easing.type: Easing.InOutQuad
        }
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.z"
            to: -6
            duration: 220
            easing.type: Easing.InOutQuad
        }
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.z"
            to: 0
            duration: 140
            easing.type: Easing.InOutQuad
        }
        PauseAnimation {
            duration: 120
        }
    }

    // --- idle_blink -------------------------------------------------------
    // Drives the eye meshes' `blink` morph target (see the manifest's
    // blink_shape_keys). Avatars without eye targets never blink. Runs
    // independently of the micro-idles, since it touches only the eyes.
    property real blinkAmount: 0   // 0 = open, 1 = closed
    onBlinkAmountChanged: if (root.controller)
        root.controller.setBlinkWeight(blinkAmount)

    SequentialAnimation {
        id: blinkAnim
        NumberAnimation {
            target: root
            property: "blinkAmount"
            to: 1
            duration: 70
            easing.type: Easing.OutQuad
        }
        PauseAnimation {
            duration: 40
        }
        NumberAnimation {
            target: root
            property: "blinkAmount"
            to: 0
            duration: 110
            easing.type: Easing.InQuad
        }
    }

    Timer {
        id: blinkTimer
        running: root.avatar !== null && root.controller !== null
        repeat: true
        interval: 3000
        onTriggered: {
            // Mostly 2.5-6.5s apart, occasionally a quick double blink.
            interval = Math.random() < 0.2 ? 260 : 2500 + Math.random() * 4000;
            // Checked each time: a hot-reloaded look replaces the nodes.
            if (!blinkAnim.running && root.controller.hasBlinkTargets())
                blinkAnim.start();
        }
    }

    // Cognitive "think" loop, active exactly while `thinking` is true. The
    // binding on idleLoop.running above means this interrupts the base
    // idle immediately.
    SequentialAnimation {
        id: thinkLoop
        loops: Animation.Infinite
        running: root.avatar !== null && root.thinking
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.z"
            from: -3
            to: 3
            duration: 900
            easing.type: Easing.InOutSine
        }
        NumberAnimation {
            target: root.avatar
            property: "eulerRotation.z"
            from: 3
            to: -3
            duration: 900
            easing.type: Easing.InOutSine
        }
    }

    // One-shot auxiliary reactions (`play_gesture` events). Names not in
    // this table are cleanly ignored -- this is a small built-in
    // transform-based reaction set rather than a baked clip lookup. All
    // properties here are rotations, deliberately -- gestureAnim's second
    // step always returns to 0, which is only correct for a rotation's
    // neutral pose, not e.g. a scale's (1.0).
    readonly property var gesturePresets: ({
            "greet": {
                property: "eulerRotation.y",
                to: 25,
                duration: 260
            },
            "agree": {
                property: "eulerRotation.x",
                to: -12,
                duration: 200
            },
            "disagree": {
                property: "eulerRotation.y",
                to: 15,
                duration: 150
            },
            "celebrate": {
                property: "eulerRotation.x",
                to: 10,
                duration: 180
            },
            "shrug": {
                property: "eulerRotation.z",
                to: 8,
                duration: 220
            }
        })

    function playGesture(name, intensity) {
        if (root.libraryMode) {
            if (root.sitState !== "" || root.floorBusy)   // it does not wave from its chair or the floor
                return;
            const clip = root.libraryGestureClip(name, intensity === undefined ? 0.5 : intensity);
            if (clip && !root.moving && playLibraryClip(clip)) {
                cancelMicroIdle();
                return;
            }
        }
        const preset = gesturePresets[name];
        if (!preset || !root.avatar)
            return;
        cancelMicroIdle();
        gestureAnim.stop();
        gestureAnim.targetProperty = preset.property;
        gestureAnim.toValue = preset.to;
        gestureAnim.stepDuration = preset.duration;
        gestureAnim.start();
    }

    SequentialAnimation {
        id: gestureAnim
        property string targetProperty: ""
        property real toValue: 0
        property int stepDuration: 200

        NumberAnimation {
            target: root.avatar
            property: gestureAnim.targetProperty
            to: gestureAnim.toValue
            duration: gestureAnim.stepDuration
            easing.type: Easing.OutQuad
        }
        NumberAnimation {
            target: root.avatar
            property: gestureAnim.targetProperty
            to: 0
            duration: gestureAnim.stepDuration
            easing.type: Easing.InQuad
        }
    }
}
