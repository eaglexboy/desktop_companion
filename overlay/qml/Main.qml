import QtQuick
import QtQuick.Window
import QtMultimedia
import Qt.labs.platform as Platform
import org.kde.layershell as LayerShellQt
import Deskmate

Window {
    id: root
    // Sized to the avatar's own footprint. The InteractionBar does not grow
    // the window: it sits inside the empty strip below the avatar's feet
    // (see the bar's anchoring below), so opening it never pushes the
    // surface off the bottom of the screen.
    width: avatarWidth
    height: avatarHeight + headroom
    color: "transparent"
    flags: Qt.FramelessWindowHint
    visible: true

    // Backend objects, handed in from main.cpp as initial properties (typed, so qmllint can check them).
    required property IpcClient ipc
    required property AudioCapture audioCapture
    required property ShortcutManager shortcutManager

    LayerShellQt.Window.layer: LayerShellQt.Window.LayerOverlay
    LayerShellQt.Window.anchors: LayerShellQt.Window.AnchorTop | LayerShellQt.Window.AnchorLeft
    // A negative exclusion zone tells the compositor this surface floats
    // above other windows without reserving any screen space for itself.
    LayerShellQt.Window.exclusionZone: -1
    LayerShellQt.Window.keyboardInteractivity: interactive ? LayerShellQt.Window.KeyboardInteractivityOnDemand : LayerShellQt.Window.KeyboardInteractivityNone
    // Output-local position of the surface. The margins follow these, so
    // animating them is what makes the companion run to a new spot instead
    // of jumping there.
    property real slideX: 24
    property real slideY: 24
    LayerShellQt.Window.margins.left: Math.round(slideX)
    LayerShellQt.Window.margins.top: Math.max(0, Math.round(slideY) - headroom)
    // Where the avatar's top edge sits inside the window: the full headroom,
    // less whatever the screen's top edge cuts off when the avatar is parked there.
    readonly property int avatarOffsetY: Math.min(Math.round(slideY), headroom)
    onAvatarOffsetYChanged: applyMask()
    readonly property bool moving: slideAnim.running
    // Per-frame, so the bubble rides along with the avatar while it runs.
    onSlideXChanged: speechBubble.avatarLeft = slideX
    onSlideYChanged: speechBubble.avatarTop = slideY

    ParallelAnimation {
        id: slideAnim
        property int durationMs: 400
        NumberAnimation {
            id: slideXAnim
            target: root
            property: "slideX"
            duration: slideAnim.durationMs
            easing.type: Easing.InOutQuad
        }
        NumberAnimation {
            id: slideYAnim
            target: root
            property: "slideY"
            duration: slideAnim.durationMs
            easing.type: Easing.InOutQuad
        }
    }

    // Moves the surface within its current output: a run when it is already
    // on screen, a jump for the very first placement or a zero duration.
    function slideTo(localX, localY, durationMs, animate, easing) {
        slideAnim.stop();
        slideStartTimer.stop();
        standMoveTimer.stop();
        if (!animate || durationMs <= 0 || (localX === slideX && localY === slideY)) {
            slideX = localX;
            slideY = localY;
            // The re-home between monitors happens mid-trip: stay side-on.
            if (!root.crossEntering)
                idleBehaviors.turnBack();
            return 0;
        }
        // A body sitting on its chair stands up first, the chair goes, and only then does it
        // turn and run: the move is held back for that long and then made as normal.
        const standMs = idleBehaviors.requestStand();
        if (standMs > 0) {
            standMoveTimer.args = [localX, localY, durationMs, animate, easing];
            standMoveTimer.interval = standMs;
            standMoveTimer.start();
            return standMs + idleBehaviors.turnMs;
        }
        const dx = localX - slideX;
        idleBehaviors.travelDir = dx >= 0 ? 1 : -1;
        // Turn to face the heading first (the slide starts once the turn is
        // done); a mostly vertical trip keeps facing the user.
        const turns = Math.abs(dx) >= 8 && idleBehaviors.turnToward(idleBehaviors.travelDir);
        slideStartTimer.interval = turns ? idleBehaviors.turnMs : 1;
        slideAnim.durationMs = durationMs;
        slideXAnim.easing.type = slideYAnim.easing.type = easing !== undefined ? easing : Easing.InOutQuad;
        slideXAnim.to = localX;
        slideYAnim.to = localY;
        slideStartTimer.start();
        return slideStartTimer.interval;
    }

    Timer {
        id: slideStartTimer
        onTriggered: slideAnim.start()
    }
    Timer {
        id: standMoveTimer
        property var args: []
        onTriggered: root.slideTo(args[0], args[1], args[2], args[3], args[4])
    }

    property bool interactive: false
    property bool hidden: false
    property bool capturingActive: false
    property string activeStreamId: ""
    property string currentLookPath: "qrc:/qt/qml/Deskmate/assets/placeholder/look.glb"
    property string currentScreenName: ""
    property int pendingManifestVersion: 0
    property bool hasFace: true
    property string activeUtteranceId: ""
    // Output the layer-shell surface is currently bound to ("" = compositor default).
    property string anchoredScreenName: ""
    // Last position the daemon asked for, so the avatar can be put back after
    // the displays disappear and return (screen lock / wake) or on demand.
    property var lastPlacement: null
    // Avatar footprint in px. The authoritative value is the user's
    // [presence] avatar_width_px/avatar_height_px, delivered in `welcome` and
    // applied in onWelcomeReceived below; the daemon uses the same values for
    // corner placement, so the two must agree. The 240 here is only a
    // fallback (it matches the config default) for the moment before
    // `welcome` arrives -- it is never what a configured size ends up as.
    property int avatarWidth: 240
    property int avatarHeight: 240
    // Transparent room above the avatar inside its window (see Avatar3D
    // `baseHeight`): the avatar's size and position on screen are unchanged.
    readonly property int headroom: Math.round(avatarHeight * 0.15)
    // [presence] show_speech_bubble, delivered in `welcome`.
    property bool showSpeechBubble: true

    // Companion voice level, [voice.tts] volume: linear 0.0-1.0 on the audio
    // output (stacks with the system mixer's volume for this stream). Delivered
    // in `welcome` and pushed live in `config_update`; anything missing or out
    // of range leaves/clamps rather than silencing the companion by accident.
    function applyVoiceConfig(config) {
        if (config && config.voice && typeof config.voice.volume === "number")
            speechOutput.volume = Math.max(0.0, Math.min(1.0, config.voice.volume));
    }
    // Sentences of the current reply spoken so far (shown in the bubble).
    property var bubbleSentences: []
    // The bubble shows the most recent sentences that fit ~240 chars, so a
    // missed sentence can still be read while the reply continues.
    readonly property string bubbleText: {
        const out = [];
        let total = 0;
        for (let i = bubbleSentences.length - 1; i >= 0; --i) {
            total += bubbleSentences[i].length + 1;
            if (out.length > 0 && total > 240)
                break;
            out.unshift(bubbleSentences[i]);
        }
        return out.join(" ");
    }
    // Speech arrives as per-sentence chunks (chunk_seq / final) that the
    // daemon sends as each one finishes synthesizing. Chunks queue here and
    // play back-to-back; speech_finished is only sent after the final chunk.
    property var speechQueue: []
    property bool activeChunkFinal: true
    property var shortcutsConfig: ({})
    // Set by main.cpp as a context property from the `welcome` handshake's
    // asset_dir field.
    // dataDir is a context property from main.cpp that may not exist yet.
    // qmllint disable unqualified
    property string deskmateDataDir: typeof dataDir !== "undefined" ? dataDir : ""
    // qmllint enable unqualified

    // Testing only (DESKMATE_FRAME_DUMP_DIR, see main.cpp): pictures of the avatar after a load.
    // qmllint disable unqualified
    readonly property string frameDumpDir: typeof testFrameDumpDir === "string" ? testFrameDumpDir : ""
    // qmllint enable unqualified
    property int frameDumpLeft: 0
    property double frameDumpFrom: 0
    Timer {
        interval: 120
        repeat: true
        running: root.frameDumpLeft > 0
        onTriggered: {
            --root.frameDumpLeft;
            const name = root.frameDumpDir + "/frame_" + String(Date.now() - root.frameDumpFrom).padStart(5, "0") + (avatar3d.concealed ? "_hidden" : "_shown") + ".png";
            avatar3d.grabToImage(function (result) {
                result.saveToFile(name);
            });
        }
    }

    WindowMaskHelper {
        id: maskHelper
    }
    LayerShellScreenHelper {
        id: layerShellScreen
    }
    // What takes the clicks: the box around the body (and the interaction bar while it is open),
    // so a click anywhere else in the window reaches what is behind the companion. Before a model
    // has loaded and been measured there is no body to go by, so the whole avatar area takes them as
    // it always did.
    property string appliedMask: ""
    function applyMask() {
        const box = avatar3d.bodyBox(0.03);
        if (!box) {
            maskHelper.applyMask(root, root.avatarWidth, root.avatarHeight, root.avatarOffsetY);
            appliedMask = "";
            return;
        }
        const left = Math.max(0, Math.round(box.x + avatar3d.x));
        const top = Math.max(0, Math.round(box.y + avatar3d.y));
        const right = Math.min(root.width, Math.round(box.x + avatar3d.x + box.width));
        const bottom = Math.min(root.height, Math.round(box.y + avatar3d.y + box.height));
        const rects = [
            {
                x: left,
                y: top,
                width: right - left,
                height: bottom - top
            }
        ];
        if (root.interactive)
            rects.push({
                x: Math.round(interactionBar.x),
                y: Math.round(interactionBar.y),
                width: Math.round(interactionBar.width),
                height: Math.round(interactionBar.height)
            });
        // Only when it changed: every call hands the compositor a new input region.
        const key = JSON.stringify(rects);
        if (key === appliedMask)
            return;
        if (maskHelper.applyRects(root, rects))
            appliedMask = key;
    }
    // The body moves, so the box follows it a few times a second.
    Timer {
        interval: 100
        repeat: true
        running: !root.hidden
        onTriggered: root.applyMask()
    }

    // The daemon broadcasts window position in global virtual-desktop
    // space, but LayerShellQt.Window.margins are relative to whichever
    // single output the layer-shell surface is anchored to. Without
    // switching the surface's output to match `screenName` and converting
    // the coordinate space, a position on a non-primary monitor (or any
    // sufficiently large global offset) pushes the window off every
    // visible monitor -- a bug that looks identical to a 3D rendering
    // failure since the window still exists, just off-screen.
    function applyPosition(x, y, screenName, hiddenFlag, durationMs) {
        const sameOutput = root.lastPlacement !== null && root.anchoredScreenName === screenName;
        root.currentScreenName = screenName;
        root.lastPlacement = {
            x: x,
            y: y,
            screen: screenName
        };
        const screens = Application.screens;
        for (let i = 0; i < screens.length; ++i) {
            if (screens[i].name === screenName) {
                // QML's QScreen exposes `virtualX`/`virtualY` -- its offset
                // within the virtual desktop -- but NOT `geometry` (reading
                // it yields undefined). Subtracting that offset converts the
                // daemon's global coordinates into this output's local
                // margin offset. The output itself is switched through the
                // C++ helper: the LayerShellQt attached `screen` property
                // rejects any QML-side screen object with "Cannot assign
                // QObject* to QScreen*", which aborted this whole function
                // and left the avatar stuck faded out.
                if (root.anchoredScreenName !== screens[i].name) {
                    // A layer-shell surface binds to its output when it is
                    // created, so switching outputs means destroying the
                    // surface (hide), pointing it at the new screen, and
                    // recreating it (show). Same-screen moves skip this.
                    root.visible = false;
                    layerShellScreen.moveToScreen(root, screens[i].name);
                    root.anchoredScreenName = screens[i].name;
                    root.slideTo(x - screens[i].virtualX, y - screens[i].virtualY, 0, false);
                    root.visible = true;
                } else {
                    // Same output: run there. (Changing output can't be
                    // animated -- a surface can't straddle two outputs.)
                    root.slideTo(x - screens[i].virtualX, y - screens[i].virtualY, durationMs || 0, sameOutput);
                }
                speechBubble.place(screens[i].name, root.slideX, root.slideY, screens[i].width, screens[i].height);
                root.hidden = hiddenFlag;
                return;
            }
        }
        // Unknown screen name (race with a monitor hot-unplug, or a
        // screen name KWin reports that this client hasn't seen yet) --
        // better to hide than to render at a stale/wrong position.
        root.hidden = true;
    }

    onInteractiveChanged: {
        applyMask();
        if (interactive)
            interactionBar.focusInput();
        root.ipc.sendInteractionState(interactive ? "active" : "idle");
    }

    // Bring the companion back no matter why it is not visible: faded out by a
    // fullscreen/hidden placement, or its surface destroyed by the compositor.
    function showCompanion() {
        root.hidden = false;
        const p = root.lastPlacement;
        root.anchoredScreenName = "";          // force a re-home to the output
        speechBubble.anchoredScreenName = "";  // the bubble's surface is lost with the displays too
        if (p)
            root.applyPosition(p.x, p.y, p.screen, false, 0);
        if (!root.visible)
            root.visible = true;
    }

    // Called from main.cpp when an output (re)appears. The compositor
    // destroys layer-shell surfaces when displays vanish, so the avatar and
    // bubble must be recreated and re-placed -- otherwise the companion stays
    // invisible after unlocking the screen.
    function recoverDisplay() {
        showCompanion();
    }

    function playNextChunk() {
        const chunk = root.speechQueue.shift();
        if (!chunk)
            return;
        root.activeUtteranceId = chunk.utterance_id || "";
        root.activeChunkFinal = chunk.final !== false;
        bubbleHideTimer.stop();
        if ((chunk.chunk_seq || 0) === 0)
            root.bubbleSentences = [];
        if (chunk.text) {
            const spoken = root.bubbleSentences.slice();
            spoken.push(chunk.text);
            root.bubbleSentences = spoken;
        }
        avatar3d.controller.setVisemeTimeline(chunk.visemes || []);
        speechPlayer.source = "file://" + chunk.wav_path;
        speechPlayer.play();
    }

    function startCapture() {
        if (root.capturingActive)
            return;
        root.activeStreamId = "stream_" + Date.now();
        root.capturingActive = true;
        // Barge-in: engaging the mic while the companion is speaking cuts
        // audio immediately rather than waiting for it to finish.
        if (speechPlayer.playbackState === MediaPlayer.PlayingState) {
            speechPlayer.stop();
            avatar3d.controller.clearVisemeWeights();
            idleBehaviors.soundWaves = false;
            root.speechQueue = [];
            root.bubbleSentences = [];
            if (root.activeUtteranceId !== "") {
                root.ipc.sendSpeechFinished(root.activeUtteranceId);
                root.activeUtteranceId = "";
            }
        }
        root.audioCapture.start(root.activeStreamId);
        root.ipc.sendAudioStreamStart(root.activeStreamId);
    }

    function stopCapture() {
        if (!root.capturingActive)
            return;
        root.capturingActive = false;
        root.audioCapture.stop();
        root.ipc.sendAudioStreamEnd(root.activeStreamId);
        root.activeStreamId = "";
    }

    Component.onCompleted: {
        applyMask();
        root.ipc.sendInteractionState("idle");
    }

    // Hidden/visible fade lives here, on an ordinary Quick Item, rather
    // than on the top-level Window's own `opacity` -- not every Wayland
    // compositor/platform plugin supports native window-level opacity, so
    // fading a window directly can silently no-op. Scene-graph-level Item
    // opacity is a Qt Quick rendering concern, not a platform/compositor
    // one, so it works everywhere.
    Item {
        id: content
        anchors.fill: parent
        opacity: root.hidden ? 0 : 1
        // No fade while crossing monitors: the run itself is the transition.
        Behavior on opacity {
            enabled: !root.crossing && !root.crossEntering
            NumberAnimation {
                duration: 200
            }
        }

        Avatar3D {
            id: avatar3d
            width: root.avatarWidth
            height: root.avatarHeight + root.headroom
            baseHeight: root.avatarHeight
            y: root.avatarOffsetY - root.headroom
            anchors.left: parent.left
            lookSource: root.currentLookPath

            onAssetLoadResult: function (ok, error) {
                if (ok) {
                    idleBehaviors.modelLoaded();
                    if (root.frameDumpDir !== "") {
                        root.frameDumpFrom = Date.now();
                        root.frameDumpLeft = 60;
                    }
                }
                root.ipc.sendAssetSwapResult(ok, root.pendingManifestVersion, ok ? "" : error);
            }
        }

        // A model with no mouth talks with sound waves beside it instead.
        SoundWaves {
            id: soundWaves
            z: 5
            unit: root.avatarWidth / 240
            x: root.avatarWidth - width - 4 * unit
            y: root.headroom + root.avatarHeight * 0.1
            active: idleBehaviors.soundWaves
        }

        AppearanceStatus {
            id: appearanceStatus
            z: 10
        }

        IdleBehaviors {
            id: idleBehaviors
            avatar: avatar3d.modelNode
            moving: root.moving
            holdTurn: root.crossing || root.crossEntering
            pivotHeight: avatar3d.pivotHeight
            controller: avatar3d.controller
            onRevealRequested: avatar3d.concealed = false
        }

        InteractionBar {
            id: interactionBar
            anchors.top: avatar3d.bottom
            // Camera framing (position y=1.0, z=4.0, 50deg FOV) puts the
            // floor line at ~77% of the view height, leaving an empty
            // transparent strip below the avatar's feet. Tuck the 48px bar
            // into that strip just under the feet (flush with the window
            // bottom at the 240px default), instead of stacking it below the
            // whole view (which left a large, dead gap -- worst when the
            // avatar sits low on screen).
            anchors.topMargin: Math.max(root.avatarHeight * 0.766 + 8, root.avatarHeight - height - 12) - root.avatarHeight
            width: root.avatarWidth
            anchors.left: parent.left
            visible: root.interactive
            pushToTalkMode: root.shortcutsConfig.push_to_talk_mode || "hold"

            onTextSubmitted: function (text) {
                root.ipc.sendUserTextInput(text);
            }
            onMicPressed: root.startCapture()
            onMicReleased: root.stopCapture()
            onMicToggled: root.capturingActive ? root.stopCapture() : root.startCapture()
        }
    }

    MediaPlayer {
        id: speechPlayer
        audioOutput: AudioOutput {
            id: speechOutput
        }
        onPositionChanged: {
            if (root.hasFace)
                avatar3d.controller.applyVisemeWeightsAt(position / 1000.0);
        }
        onPlaybackStateChanged: {
            idleBehaviors.soundWaves = !root.hasFace && playbackState === MediaPlayer.PlayingState;
            idleBehaviors.speaking = playbackState === MediaPlayer.PlayingState;
            if (playbackState === MediaPlayer.StoppedState && root.activeUtteranceId !== "") {
                avatar3d.controller.clearVisemeWeights();
                if (root.speechQueue.length > 0) {
                    root.playNextChunk();
                } else if (root.activeChunkFinal) {
                    root.ipc.sendSpeechFinished(root.activeUtteranceId);
                    root.activeUtteranceId = "";
                    bubbleHideTimer.restart();  // linger so the last sentence can be read
                }
                // else: a non-final chunk finished before the next one was
                // synthesized -- stay armed; onSpeechReceived resumes playback.
            }
        }
    }

    // Crossing to another monitor: a layer-shell surface can't straddle two
    // outputs, so the companion runs off the edge of the current one (fading
    // as it goes), is re-homed while invisible, then runs in from the matching
    // edge of the new one -- the same trip the eye would expect from a walk
    // between side-by-side (or stacked) monitors, instead of a teleport.
    readonly property int crossRunMs: 650
    property bool crossing: false
    property bool crossEntering: false
    property var crossTarget: null   // { x, y, screen, entryX, entryY } -- output-local entry edge

    function screenByName(name) {
        const screens = Application.screens;
        for (let i = 0; i < screens.length; ++i)
            if (screens[i].name === name)
                return screens[i];
        return null;
    }

    // Returns false when the run can't be staged (first placement, hidden,
    // unknown screen), so the caller falls back to the plain fade.
    function crossTo(x, y, screenName, hiddenFlag) {
        const from = screenByName(root.anchoredScreenName);
        const to = screenByName(screenName);
        if (!from || !to || hiddenFlag || root.hidden || root.lastPlacement === null)
            return false;
        const dx = (to.virtualX + to.width / 2) - (from.virtualX + from.width / 2);
        const dy = (to.virtualY + to.height / 2) - (from.virtualY + from.height / 2);
        const horizontal = Math.abs(dx) >= Math.abs(dy);
        const localY = Math.max(0, Math.min(y - to.virtualY, to.height - root.avatarHeight));
        const localX = Math.max(0, Math.min(x - to.virtualX, to.width - root.avatarWidth));
        let exitX = root.slideX, exitY = root.slideY, entryX = localX, entryY = localY;
        if (horizontal) {
            exitX = dx > 0 ? from.width - root.avatarWidth : 0;
            entryX = dx > 0 ? 0 : to.width - root.avatarWidth;
        } else {
            exitY = dy > 0 ? from.height - root.avatarHeight : 0;
            entryY = dy > 0 ? 0 : to.height - root.avatarHeight;
        }
        root.crossTarget = {
            x: x,
            y: y,
            screen: screenName,
            entryX: entryX + to.virtualX,
            entryY: entryY + to.virtualY
        };
        root.crossing = true;
        // The run starts after the turn; the swap waits for both.
        const lead = root.slideTo(exitX, exitY, root.crossRunMs, true, Easing.InQuad);
        crossTimer.interval = lead + root.crossRunMs;
        crossTimer.restart();
        return true;
    }

    Timer {
        id: crossTimer
        // Swap once the exit run has reached the edge. It ends at full speed
        // (ease-in), so the re-home reads as leaving the screen, not a stop.
        interval: root.crossRunMs
        onTriggered: {
            const t = root.crossTarget;
            root.crossTarget = null;
            if (!t) {
                root.crossing = false;
                return;
            }
            // Re-home at the entry edge (a jump while invisible). The surface
            // is destroyed and recreated for the new output, which takes a
            // moment to appear: the run in waits for its first frame, or the
            // whole run would play before anything is on screen.
            root.crossEntering = true;
            root.hidden = true;
            root.applyPosition(t.entryX, t.entryY, t.screen, false, 0);
            root.lastPlacement = {
                x: t.x,
                y: t.y,
                screen: t.screen
            };
            root.entryRun = t;
            root.awaitFrame = true;
            entryFallbackTimer.restart();
            root.crossing = false;
        }
    }

    // The run in on the new output, once its surface is up.
    property var entryRun: null
    property bool awaitFrame: false
    function startEntryRun() {
        const t = root.entryRun;
        root.entryRun = null;
        root.awaitFrame = false;
        entryFallbackTimer.stop();
        entrySettleTimer.stop();
        idleBehaviors.applyProp();   // the recreated surface brought the scene back with the chair showing
        if (!t)
            return;
        const to = root.screenByName(t.screen);
        let ran = 0;
        if (to)
            ran = root.slideTo(t.x - to.virtualX, t.y - to.virtualY, root.crossRunMs, true, Easing.OutQuad);
        root.crossEntering = false;
        // Already at the spot, so no run in to end the trip: face the user now.
        if (ran === 0)
            idleBehaviors.turnBack();
    }
    Timer {
        id: entrySettleTimer
        interval: 90
        onTriggered: root.startEntryRun()
    }
    Timer {
        id: entryFallbackTimer
        interval: 800
        onTriggered: root.startEntryRun()
    }
    Connections {
        target: root
        function onFrameSwapped() {
            if (root.awaitFrame) {
                root.awaitFrame = false;
                entrySettleTimer.restart();
            }
        }
    }

    Timer {
        id: migrateTimer
        interval: 200
        property real targetX: 0
        property real targetY: 0
        property string targetScreen: ""
        property bool targetHidden: false
        onTriggered: root.applyPosition(targetX, targetY, targetScreen, targetHidden, 0)
    }

    Connections {
        target: root.audioCapture
        function onChunkReady(streamId, seq, pcm16le) {
            root.ipc.sendAudioStreamChunk(streamId, seq, pcm16le);
        }
    }

    Connections {
        target: root.shortcutManager
        function onToggleInteractionTriggered() {
            root.interactive = !root.interactive;
        }
        function onPushToTalkPressed() {
            root.startCapture();
        }
        function onPushToTalkReleased() {
            root.stopCapture();
        }
        function onPushToTalkToggled() {
            root.capturingActive ? root.stopCapture() : root.startCapture();
        }
    }

    Connections {
        target: root.ipc

        function onWelcomeReceived(manifest, config, assetDir) {
            root.deskmateDataDir = assetDir;
            root.applyVoiceConfig(config);
            root.shortcutsConfig = config.shortcuts || {};
            if (config.presence && config.presence.avatar_width_px > 0 && config.presence.avatar_height_px > 0) {
                root.avatarWidth = config.presence.avatar_width_px;
                root.avatarHeight = config.presence.avatar_height_px;
                root.applyMask();
            }
            root.showSpeechBubble = !config.presence || config.presence.show_speech_bubble !== false;
            const versions = manifest.versions || [];
            const current = versions.find(function (v) {
                return v.version === manifest.current_version;
            });
            if (current) {
                root.pendingManifestVersion = current.version;
                root.hasFace = current.has_face;
                avatar3d.controller.shapeKeyNames = current.viseme_shape_keys || [];
                avatar3d.controller.blinkKeyNames = current.blink_shape_keys || [];
                avatar3d.controller.runKeyNames = current.run_shape_keys || [];
                avatar3d.controller.gestureKeyNames = current.gesture_shape_keys || [];
                // A model whose own bones carry the idle clips plays them; the rest keep the transform idles.
                idleBehaviors.preferBakedClips = current.skeletal_clips === true;
                // A body wearing an animation library plays its clips by role (idle, talk, run...).
                idleBehaviors.libraryClips = current.library_clips || ({});
                avatar3d.pivotHeight = ((current.bounds && current.bounds.height_m) || 0) * 0.45;
                avatar3d.fitScale = (current.bounds && current.bounds.fit_scale) || 1;
                root.currentLookPath = "file://" + assetDir + "/" + current.glb_filename;
            }
            if (config.shortcuts) {
                root.shortcutManager.registerToggleInteraction(config.shortcuts.toggle_interaction);
                root.shortcutManager.registerPushToTalk(config.shortcuts.push_to_talk, config.shortcuts.push_to_talk_mode);
            }
        }

        // Pushed when config.toml changes ([voice.tts] volume, live).
        function onConfigUpdateReceived(config) {
            root.applyVoiceConfig(config);
        }

        function onPositionChanged(x, y, screenName, durationMs, hiddenFlag) {
            // Moving across monitors triggers a 200ms fade-out, re-anchor,
            // 200ms fade-in sequence to avoid compositor re-map flicker.
            // Same-screen moves slide over duration_ms.
            if (root.entryRun !== null) {
                // Waiting for the new surface: only the spot can change.
                if (screenName === root.entryRun.screen)
                    root.entryRun = {
                        x: x,
                        y: y,
                        screen: screenName
                    };
                return;
            }
            if (root.crossing) {
                // Mid-run: retarget. Same destination keeps its entry edge;
                // a different one just appears at its target.
                const t = root.crossTarget;
                const to = root.screenByName(screenName);
                if (t && to) {
                    const sameDest = screenName === t.screen;
                    root.crossTarget = {
                        x: x,
                        y: y,
                        screen: screenName,
                        entryX: sameDest ? t.entryX : x,
                        entryY: sameDest ? t.entryY : y
                    };
                }
                return;
            }
            if (root.currentScreenName !== "" && screenName !== root.currentScreenName) {
                if (root.crossTo(x, y, screenName, hiddenFlag))
                    return;
                root.hidden = true;
                migrateTimer.targetX = x;
                migrateTimer.targetY = y;
                migrateTimer.targetScreen = screenName;
                migrateTimer.targetHidden = hiddenFlag;
                migrateTimer.start();
            } else {
                root.applyPosition(x, y, screenName, hiddenFlag, durationMs);
            }
        }

        function onSpeechReceived(payload) {
            idleBehaviors.thinking = false;
            const startsNewUtterance = (payload.chunk_seq || 0) === 0;
            if (startsNewUtterance)
                root.speechQueue = [];  // a new reply preempts whatever was queued
            root.speechQueue.push(payload);
            if (speechPlayer.playbackState === MediaPlayer.PlayingState) {
                // Mid-sentence: a continuation just waits its turn; a new
                // utterance cuts in (the Stopped handler starts the queue head).
                if (startsNewUtterance)
                    speechPlayer.stop();
            } else {
                root.playNextChunk();
            }
        }

        function onHotReloadAsset(manifest, glbUrl) {
            const versions = manifest.versions || [];
            const current = versions.find(function (v) {
                return v.version === manifest.current_version;
            });
            if (current) {
                root.pendingManifestVersion = current.version;
                root.hasFace = current.has_face;
                avatar3d.controller.shapeKeyNames = current.viseme_shape_keys || [];
                avatar3d.controller.blinkKeyNames = current.blink_shape_keys || [];
                avatar3d.controller.runKeyNames = current.run_shape_keys || [];
                avatar3d.controller.gestureKeyNames = current.gesture_shape_keys || [];
                // A model whose own bones carry the idle clips plays them; the rest keep the transform idles.
                idleBehaviors.preferBakedClips = current.skeletal_clips === true;
                // A body wearing an animation library plays its clips by role (idle, talk, run...).
                idleBehaviors.libraryClips = current.library_clips || ({});
                avatar3d.pivotHeight = ((current.bounds && current.bounds.height_m) || 0) * 0.45;
                avatar3d.fitScale = (current.bounds && current.bounds.fit_scale) || 1;
                root.currentLookPath = "file://" + root.deskmateDataDir + "/" + current.glb_filename;
            }
        }

        function onStatusUpdateReceived(payload) {
            idleBehaviors.thinking = payload.status === "thinking";
        }

        function onAppearanceProgressReceived(payload) {
            appearanceStatus.update(payload);
        }

        function onMoodUpdateReceived(payload) {
            if (payload.mood)
                idleBehaviors.mood = payload.mood;
        }

        function onGestureReceived(payload) {
            idleBehaviors.playGesture(payload.name || "", payload.intensity);
        }
    }

    // Dismiss interactive mode on Escape, or when the window loses focus.
    Shortcut {
        sequence: "Escape"
        enabled: root.interactive
        onActivated: root.interactive = false
    }
    // Opening the bar resizes the window, and the compositor briefly drops
    // and regains focus during that -- acting on the first loss closed the
    // bar the instant it opened. Only dismiss if focus is still lost after
    // a short settle delay (a real click-away stays lost).
    // System tray (StatusNotifier) icon: the manual way to bring the
    // companion back when it is hidden, and to talk to it or quit.
    Platform.SystemTrayIcon {
        id: tray
        visible: true
        icon.name: "face-smile"
        tooltip: "Deskmate companion"
        onActivated: function (reason) {
            if (reason === Platform.SystemTrayIcon.Trigger)
                root.showCompanion();
        }
        menu: Platform.Menu {
            Platform.MenuItem {
                text: "Show companion"
                onTriggered: root.showCompanion()
            }
            Platform.MenuItem {
                text: root.interactive ? "Close input" : "Talk to companion…"
                onTriggered: {
                    root.showCompanion();
                    root.interactive = !root.interactive;
                }
            }
            Platform.MenuSeparator {}
            Platform.MenuItem {
                text: "Quit"
                // Exit code 3 is excluded from the service's restart policy,
                // so this is the one way to stop the overlay for good.
                onTriggered: Qt.exit(3)
            }
        }
    }

    Timer {
        id: bubbleHideTimer
        interval: 3500
        onTriggered: root.bubbleSentences = []
    }

    SpeechBubble {
        id: speechBubble
        text: root.bubbleText
        enabled: root.showSpeechBubble
        avatarHidden: root.hidden
        avatarWidth: root.avatarWidth
        avatarHeight: root.avatarHeight
    }

    Timer {
        id: focusLossTimer
        interval: 250
        onTriggered: {
            if (root.interactive && !root.active)
                root.interactive = false;
        }
    }
    onActiveChanged: {
        if (root.interactive && !root.active)
            focusLossTimer.restart();
        else
            focusLossTimer.stop();
    }
}
