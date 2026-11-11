# Spec: Presentation Layer & Desktop Sensing (`overlay/`, `adapters/`)

## Component Role & Architectural Separation

The Deskmate Presentation and Sensing subsystems constitute the user-facing and desktop-sensing boundaries of the system. They are partitioned into two decoupled roles:

1. **Presentation Client (Overlay):** An agnostic graphical client that displays the 3D avatar on screen, renders real-time facial visemes and body gestures synchronized to local audio playback, handles non-blocking window evasion with movement animations (`move`), indicates cognitive states (`think`), and provides an interactive mode for text input and microphone audio streaming.
2. **Desktop Sensing Adapter:** A platform-specific script or daemon extension that monitors window focus, geometries, monitor topologies, and workspace state, dispatching debounced telemetry to the Brain daemon over an abstracted telemetry bus (`org.deskmate.Presence`).

Both components interact with the Brain daemon strictly through open contracts: standard WebSocket JSON event envelopes for the presentation client and an abstracted telemetry RPC schema for desktop sensing.

---

## Scope: V1 Reference Implementations vs. Engine Agnosticism

### Core Agnostic Architecture (Universal)
* The protocol, event envelopes, asset formats (`.glb`), morph target taxonomies, and coordinate systems are completely platform- and engine-agnostic.
* Any frontend client capable of rendering glTF 2.0 assets, morphing shape keys by name, playing audio, and consuming WebSockets can serve as the Deskmate presentation layer (e.g., Godot, Tauri/WebGPU, native Windows/macOS shells in future revisions).
* Any desktop environment or window manager capable of emitting bounding-box coordinates to the telemetry bus can serve as the sensing backend.

### V1 Reference Implementations
* **V1 Presentation Client (`overlay/`):** Implemented in **C++20 and Qt 6.8+ LTS** (`QtQuick3D`, `LayerShellQt`, `QtWebSockets`, `QtMultimedia`).
* **V1 Desktop Sensing Adapter (`adapters/kwin/`):** Implemented as a **KDE Plasma 6 KWin JavaScript** automation script interacting via session D-Bus. Future platforms add sibling directories under `adapters/` (e.g. `adapters/gnome/`, `adapters/macos/`) implementing the same telemetry RPC schema.

---

## Agnostic Presentation Client Specification

### 1. Windowing, Transparency & Canvas Model
* **Non-Intrusive Presence:** Sized by default to `avatar_width_px` $\times$ `avatar_height_px` (standard baseline: $320\times 320\,\text{px}$).
* **Wayland Layer Shell Placement:** Canvas is pinned to `LayerShellQt::Window::LayerOverlay` with `exclusiveZone = -1` (floats above applications without reserving screen work area).
* **Wayland Local Offsets:** Coordinates $(x, y)$ from `set_position` are interpreted as local pixel offsets within the specified output/screen.
  * Moving across monitors triggers a clean fade-out ($200\,\text{ms}$) $\to$ re-anchor to the target `QScreen` $\to$ fade-in ($200\,\text{ms}$) sequence to avoid compositor re-map flicker.
* **Focus & Click-Through Policy:**
  * **Passive Mode:** The window sets `KeyboardInteractivity = None`. An input region mask (`QWindow::setMask`) is applied matching the avatar's screen bounding rectangle (`QRect(0, 0, 320, 320)`). Clicks outside the avatar pass through cleanly to underlying applications.
  * **Interactive Mode (`InteractionBar`):**
    * When summoned, the canvas expands its input mask dynamically to encompass both the avatar and the anchored `InteractionBar` (`QRect(0, 0, 320, 376)`).
    * Layout metrics: anchored directly below the avatar with an $8\,\text{px}$ vertical gap; width $320\,\text{px}$, height $48\,\text{px}$. Controls include a single-line text input field ($264\,\text{px}$ width) and a Push-to-Talk button ($36\times 36\,\text{px}$ with microphone icon).
    * The window switches to `KeyboardInteractivity = OnDemand` and acquires keyboard focus.
  * **Dismissal:** Dismissed via `Escape` key, clicking outside the window (detected via `focusOutEvent`), or pressing global shortcut `toggle_interaction`. On dismissal, the interaction bar collapses, the input mask contracts to the avatar bounds (`QRect(0, 0, 320, 320)`), and the window returns to `KeyboardInteractivity = None`.
* **Fullscreen Evasion & Hiding:** When receiving `hidden: true`, the client fades out cleanly; it restores visibility upon receiving `hidden: false`.

### 2. Locomotion & Non-Blocking Repositioning
* The canvas smoothly interpolates its screen position over `duration_ms` using ease-out motion curves.
* **Evasion Motion (`move`):** While translating across coordinates ($\Delta(x, y) > 0$), the client triggers the `move` animation action for the duration of the translation, transitioning back into the active idle upon arrival. If the active model lacks a `move` clip, it falls back to a procedural lean/squash in the direction of travel.

### 3. Dual Speech & Lip Sync Playback
Speech is delivered as complete or sentence-chunked utterance payloads (`speech`):
```json
{
  "utterance_id": "utt_123",
  "chunk_seq": 0,
  "final": true,
  "wav_path": "<deploy_dir>/scratch/utt_123.wav",
  "audio_url": "/audio/utt_123.wav",
  "text": "Hello! How can I help you?",
  "delivery": "animated",
  "visemes": [
    {"start_s": 0.00, "end_s": 0.08, "shape_key": "viseme_X"},
    {"start_s": 0.08, "end_s": 0.15, "shape_key": "viseme_A"}
  ]
}
```

* **Frame Timing & Clock Sampling Guarantees:**
  * Target render frame rate is **60 FPS** (vsync-synchronized via Wayland presentation clock).
  * Maximum acceptable viseme-to-audio synchronization latency is $\le 20\,\text{ms}$ (within 1 frame at 50–60 Hz).
  * **Frame-Rate Independence & Slow Renderers:** Morph weights are calculated by direct functional lookup $W(t_{\text{elapsed}})$ on each render tick, rather than incremental delta-stepping. If system load or compositor contention drops rendering to $15\text{--}30\,\text{FPS}$, the animation evaluates directly at the current audio timestamp without accumulating drift.
* **Audio Clock Discontinuity & Buffer Under-run Handling:**
  * If `QMediaPlayer` experiences audio buffer under-runs, pipeline stalls, or device switching causing a time discontinuity ($\vert \Delta t_{\text{audio}} - \Delta t_{\text{wallclock}} \vert > 100\,\text{ms}$), the attack/decay envelope interpolation snaps immediately to the target weight for the active viseme at the new timestamp rather than gliding across the gap.
  * Standard interpolation envelope: asymmetric attack/decay ($20\,\text{ms}$ attack to $1.0$, $30\,\text{ms}$ decay to $0.0$).
* **Delivery Style Blending:** Layers the corresponding `talk_*` action (`subdued`, `animated`, `explanatory`, `deadpan`) over the speech duration.
* **Faceless Mode (`has_face: false`):** A model with no mouth does not pulse or move for speech, since that looks wrong. While the speech audio plays, the overlay shows a speech indicator beside the model: a few arcs of growing size, animated to read as sound travelling outwards, that fade in and out with the speech. It stays inside the avatar's window, so the window keeps the avatar's footprint, and it scales with the avatar. The model's idle motions pause for the duration, as they do for any spoken reply, and its talking clip from the animation library still plays.
* **Barge-In:** If the user engages push-to-talk while the companion is speaking, the client immediately cuts audio playback, flushes pending chunks, and emits `speech_finished: {"utterance_id": str, "interrupted": true}`.

### 4. Microphone Audio Streaming Wire Format
* **Format:** Configured via `QAudioSource` using `QAudioFormat` (SampleRate = 16000, ChannelCount = 1, SampleFormat = Int16).
* **Wire Protocol:** Sends `audio_stream_start: {stream_id, sample_rate: 16000, encoding: "pcm16le", channels: 1}`, followed by raw binary WebSocket messages (20–40ms PCM frames) accompanied by JSON `audio_stream_chunk: {stream_id, seq}`, concluding with `audio_stream_end: {stream_id}` upon push-to-talk release.

### 5. Layered Animation State Machine
The character driver blends animations across concurrent tiers:
* **Tier 5: Auxiliary Reactions (One-Shot):** `greet`, `agree`, `disagree`, `celebrate`, `shrug`. Missing clips are cleanly ignored.
* **Tier 4: Conversational Delivery:** `talk_subdued`, `talk_animated`, etc.
* **Tier 3: Cognitive States:** `think` loop active on `status: "thinking"`. Interrupted immediately by speech.
* **Tier 2: Mood Idles:** Selected based on `mood_update` (`idle_bored`, `idle_curious`, `idle_fidget`, etc.).
* **Tier 1: Base Idle Loop:** Continuous looping baseline (`idle_lookaround`, `idle_stretch`, `idle_blink`).
* **Limb motion on a rigged imported model:** driven by morph weights. A mesh that is moved by both the legs and the arms (a whole body in one mesh) carries the two run targets and then the four gesture targets, six in all; the controller reads a mesh's targets by count and order, so 6 means run first, gesture after. (The earlier note that the loader does not apply a rig's bone clips was about the generator's clips, which drive bones an imported rig lacks; Qt 6.10 does play a rig's own clips as Timeline animations, see the next bullet.)
* **Skeletal idle clips on an imported rig:** when the manifest entry says `skeletal_clips: true`, the generator baked `idle_lookaround`, `idle_stretch`, `idle_peek`, `idle_sleepy`, `idle_fidget`, `agree` and `disagree` on the model's own head, neck and chest bones (rotations given about world axes and converted into each bone's rest frame, so bone orientation does not matter). The overlay stops the auto-playing Timelines after load and plays one clip at a time by name (`preferBakedClips`, set per model in `Main.qml`); other models keep the transform idles. Only a rig with a real head bone gets them (a rig whose trunk stands in for the head would swivel its whole body, so it keeps the transform idles). Checked on a Quaternius body in a standalone Quick3D probe (head turns, nods, tilts, with the mouth, brows and glasses following), and head turns were seen in the running overlay.
* **Animation library:** when the manifest entry carries `library_clips` (role → clip; ADR_ACCESSORY_MODELS 14) the body wears an animation library, and `IdleBehaviors.qml` plays those clips instead of the transform stand-ins and the morph-target limb poses. The brain sends `skeletal_clips` and `library_clips` in the manifest. Every behaviour below needs its role's clip; a body without it simply does not do that.
  * **Base behaviours.** `idle` loops whenever nothing else is going on; `talk` plays while speaking; `run` while moving (the 90 degree turn toward the heading still happens first; the hop and the sway are off). The mood chooses variations: `idle_alt` once in a while when it is bored, `idle_sad` as the standing idle when it is sad, and the look-around micro idle (and a peek) plays `idle_look` unless it is bored (then `idle_alt`).
  * **Options.** A role may have several clips (`<role>.alt1`...). `pickOption(role)` picks one at random, kept for as long as the occasion lasts (one reply for `talk`, one sleep for `sleep`).
  * **Gestures.** The `play_gesture` payload may carry `intensity` (0..1, from the companion's mood; 0.5 at rest). With a library, `greet`, `celebrate`, `angry` and `hip_thrust` play their clips, not while it sits or runs; the other gestures keep the transform presets. A gesture picks an option, then its intensity variant (`.high` from 0.65, `.low` up to 0.35, when the clip has them) and, half the time, its `.mirror` copy (as does a seated variation), so it is not always the same hand.
  * **Sitting.** After 4-12 minutes of idling it sits down: the enter clip, the idle loop for 1-4 minutes, the exit clip. It sits on a chair (`sit_*`, with the chair) or on the floor (`floor_sit_*`: no chair, no seated variation), chosen at random each time among the sets the body has. A body may have several ways of sitting on the floor (`floor_sit_*`, `floor_sit_*.alt1`...); each is a set (enter, idle and exit begin and end in the poses the next needs), so the one picked at the start (`floorVariant`) is played to the end. While seated on a chair it plays a seated variation (`sit_alt`) every 40-120 s, and stands up after it if asked to meanwhile. It stands up first, playing the exit clip, before it moves, and before it speaks or thinks too, unless it is on a chair and has `sit_talk`: then it stays seated, talks with that clip (a seated variation in progress is cut short) and stands up once it has finished (`staysSeated`, `talkedSeated`).
  * **The chair.** When the manifest's `library_clips.sit_prop` names a node it is a mesh with three morph targets (the first collapses it), hidden after every load and driven by `AvatarController::setSitPropShown(amount)`: grown in from the start of the enter clip and shrunk away halfway through the exit clip (300 ms each). Speech drives every other mesh by morph-target index, so the chair (like the blink, run and gesture meshes) is left out of the visemes: driven as a mouth it would reappear whenever the mouth was closed. Moving to another output destroys and recreates the surface, and the rebuilt scene starts with the chair showing, so the chair's hidden state is also re-applied by the 700 ms re-check timer below and once the new output's first frame is up (`Main.qml`).
  * **Asking for attention.** Left alone standing for a while it tries to get the user's attention with `attention` (a raised arm, both arms up; options): once after 3-8 minutes without anything said, moved or thought, and then now and then (10-25 minutes) for as long as it is ignored. Anything that happens starts the wait again, and it does not do it sitting, asleep or working out.
  * **Thinking hard.** When it has been thinking for a long time (6 s, and again every 20 s while it goes on) it plays `think_long` (a hand to the head: it does not know the answer); the reply that ends the thought cuts it short.
  * **Moving from a seat.** A move asked for while it sits (`Main.qml` `slideTo`) first asks `IdleBehaviors.requestStand()`, which stands it up and returns the ms that takes; the turn and the run are held back that long, so it never turns or runs on the chair.
  * **Sleeping.** Gone for a long time (no speech, move or thinking for 30-60 minutes; `lastUserActivity`, which standing up from the chair does not reset) it sleeps: `lie_down`, then the `sleep` loop, with `sleep_alt` now and then (a slow shift in its sleep: the clip turns part of the way and back along the same path, ending in the pose it began in; the first 2.5-5 minutes after lying down, then every 3-8). A body may have several ways of sleeping (`sleep`, `sleep.alt1`...), each a set of `lie_down`, `sleep`, `sleep_alt`, `wake` and `wake_slow` clips that begin and end in the poses the next needs: the way picked when it lies down (`sleepVariant`, the suffix of the option) is the one whose lie-down, shift and get-up are played, and a way with no shift or no slow get-up simply has none (it gets up with the quick one). Anything that starts (speech, a move, thinking) has it get up first with `wake` (the quick get-up), which ends standing like the idle, before the speech, the turn and the run; a move waits for that (`requestStand` returns how long). From the chair it stands up first. Left alone it sleeps for 45-120 minutes and then gets up by itself with `wake_slow` (the slow get-up from lying flat), and sleeps again after the next long idle.
  * **Working out.** Now and then (10-30 minutes) it works out when standing idle: one of the clips of `workout` and `workout_floor`, all of them options (each lies down, does the exercise and stands again), picked at random; nothing interrupts it. Speech that starts meanwhile is answered when it is up (a move is held back, as for sitting), and gestures are skipped on the floor.
  * **One clip at a time** (`AvatarController::playClip`). A clip that is not playing has its timeline disabled (all of them write the same bones). A finite clip rewinds itself when done, except a library one-shot, played with `hold`, which stays on its last pose until the next clip starts: a rewind drops the body into the rest pose, a T-pose, for the moment before the next clip. A timer re-checks every 700 ms, so a reload or reset cannot leave the body in its rest pose; it also re-applies the chair's hidden state. A freshly loaded look is kept hidden until its first clip is playing (the loader's success starts the clip at once, a delay of 300 ms lets it take hold, and a short delay lets it take hold; the clip cannot start until the model's animations exist, which on a big model is some time after the load reports success, and they are made one after another, so the clip is started once, when the number of animations it has (`AvatarController::clipAnimationCount`) has stopped growing for three checks 100 ms apart, and started once more, still hidden, just before it is revealed (a clip counts as playing only when every one of its animations runs: `clipRunning`) (starting earlier leaves the late ones, an arm for instance, in the bind pose until the clip next restarts), and an 8 s cap reveals the avatar regardless), so the rest pose is never seen on start-up or after a look change.
  * **Testing.** Each behaviour can be brought forward with an environment variable that must be in the *service's* environment (`systemctl --user set-environment NAME=N`, then restart; `VAR=x systemctl ...` does not reach the service). Unset, nothing changes.
    * `DESKMATE_SIT_TEST_SECONDS=N`: sit after N idle seconds, for N seconds (checked every 2 s).
    * `DESKMATE_SLEEP_TEST_SECONDS=N`: sleep after N idle seconds, shift after 1.2 N and wake by itself after 3 N (once a shift in progress is over); waking restarts the idle timer, so it sleeps again after N more seconds for as long as the variable is set.
    * `DESKMATE_WORKOUT_TEST_SECONDS=N`: work out every N seconds (checked every 2 s).
    * `DESKMATE_ATTENTION_TEST_SECONDS=N`: ask for attention every N seconds (checked every 2 s).
* **Limb motion (earlier note, now superseded for the above):** While the avatar runs, its `run_shape_keys` pair is cross-faded in time with the hop, as for generated avatars. While it speaks, its `gesture_shape_keys` (`gesture_l_out`/`_l_in`, `gesture_r_out`/`_r_in`) are driven in phrases: one arm, the other or both swings between its two extremes for a second or two (an envelope eases it in and out), then both settle for a pause. Gestures stop while it runs and are skipped for a model without those targets. When it is just standing (not speaking, running, thinking or turning) the same targets are driven gently: a slow drift of each arm (about 15-30% of the swing, a 4-8 s cycle, re-rolled every 5-11 s) with, now and then, a bigger wave or shift (about 50-80% for 2-3 s); the strengths and the chance of the bigger move scale with mood (fidgeting more, bored least). Anything that takes over the body eases the arms back to rest.

---

## Global Shortcuts & Registration Architecture

* **V1 Reference Implementation:** In native KDE Plasma 6 desktop deployments, `shortcut_manager.cpp` registers global hotkeys directly via session D-Bus with KDE's `org.kde.kglobalaccel` service, eliminating sandboxing portal ceremonies. The XDG Desktop Portal is reserved as a fallback for sandboxed builds.
* **Shortcut Actions:**
  * `toggle_interaction` (`Meta+Alt+D`): Expands/focuses `InteractionBar`.
  * `push_to_talk` (`Meta+Alt+M`): Listens for key-down (`audio_stream_start`) and key-up (`audio_stream_end`) in `push_to_talk_mode = "hold"`, or toggles recording in `"toggle"` mode.

---

## Agnostic WebSocket IPC Protocol (`ipc_client.*`)

Connects to `ws://<server.host>:<server.port>/ws/overlay`.

### 1. Connection Handshake
1. **Client `hello`:**
   ```json
   {
     "type": "hello",
     "timestamp": 1718000000.0,
     "payload": {
       "protocol_version": 1,
       "client_type": "overlay",
       "client_id": "desktop_primary",
       "capabilities": ["audio_in", "audio_out", "3d_glb", "shared_filesystem", "desktop_presence"]
     }
   }
   ```
2. **Daemon `welcome`:**
   ```json
   {
     "type": "welcome",
     "timestamp": 1718000000.1,
     "payload": {
       "protocol_version": 1,
       "asset_dir": "<deploy_dir>/data/current",
       "manifest": {
         "current_version": 3,
         "versions": [
           {
             "version": 3,
             "glb_filename": "look-v3.glb",
             "sha256": "4a7b9c...",
             "created_at": 1718000000.0,
             "status": "ok",
             "has_face": true,
             "speech_indicator_mode": "viseme_shapekeys",
             "capabilities": {"has_head": true, "has_arms": true, "has_legs": true, "has_face": true},
             "viseme_shape_keys": ["viseme_A", "viseme_B", "viseme_C", "viseme_D", "viseme_E", "viseme_F", "viseme_G", "viseme_H", "viseme_X"],
             "animation_clips": [{"name": "idle_lookaround", "loop": true, "duration_s": 4.0}, {"name": "move", "loop": true, "duration_s": 1.0}, {"name": "think", "loop": true, "duration_s": 3.0}],
             "bounds": {"height_m": 1.65, "width_m": 0.55, "depth_m": 0.32}
           }
         ]
       },
       "config": {
         "presence": {"avatar_width_px": 320, "avatar_height_px": 320},
         "shortcuts": {"toggle_interaction": "Meta+Alt+D", "push_to_talk": "Meta+Alt+M", "push_to_talk_mode": "hold"}
       }
     }
   }
   ```
   * **Mandatory Manifest Subset:** Each entry in `manifest.versions[]` must provide `glb_filename`, `has_face`, `viseme_shape_keys`, `animation_clips`, and `bounds`. If any mandatory field is missing, the client rejects the asset and emits `asset_swap_result: {ok: false}`.

### 2. Concrete Wire Payloads for `asset_swap_result`
When the client attempts to mount a new `.glb` container received via `hot_reload_asset` or during handshake, it responds with:
* **Success Envelope:**
  ```json
  {
    "type": "asset_swap_result",
    "timestamp": 1718000000.5,
    "payload": {
      "ok": true,
      "version": 3,
      "error": null
    }
  }
  ```
* **Failure Envelope:**
  ```json
  {
    "type": "asset_swap_result",
    "timestamp": 1718000000.5,
    "payload": {
      "ok": false,
      "version": 3,
      "error": "GLTF parsing failed: missing root armature node"
    }
  }
  ```

### 3. Events Summary
* **Daemon $\to$ Client:** `welcome`, `chat_response`, `set_position`, `speech`, `hot_reload_asset`, `status_update`, `mood_update`, `play_gesture`, `config_update`.
* **Client $\to$ Daemon:** `hello`, `user_text_input`, `audio_stream_start`, `audio_stream_chunk`, `audio_stream_end`, `asset_swap_result`, `speech_finished`, `interaction_state`.

---

## V1 Reference Implementation Details

### 1. V1 Desktop Overlay Architecture (`overlay/` — Qt 6.8+ LTS / C++20)

#### Dependencies & Third-Party Fallback
`CMakeLists.txt` links Qt 6 (`Quick3D`, `LayerShellQt`, `WebSockets`, `Multimedia`) and includes `tinygltf` via `FetchContent` to ensure an immediate fallback if Qt's `RuntimeLoader` fails morph or animation blending validations during Phase-0 de-risking spikes.

#### Asset Failure & Malformed Binary Invariant
If a loaded asset fails parsing or exhibits invalid glTF magic bytes, the client remains running in a zero-opacity transparent state, preserves its active WebSocket connection to the daemon, and emits `asset_swap_result: {"ok": false, "version": N, "error": "<reason>"}` without crashing.

#### Phase-0 Rendering De-Risking Spike
1. Loads an exported test `.glb` containing skeleton, 9 canonical Rhubarb shape keys, and 3 animation clips.
2. Asserts morph target indexing and dynamic weight assignment from C++ at 60 fps.
3. Tests animation clip blending across base and cognitive tiers.
4. *Fallback Guarantee:* If Qt's `RuntimeLoader` morph/blending APIs exhibit driver-level defects, `avatar_controller.cpp` engages the vendored `tinygltf` parser to construct `QQuick3DGeometry` nodes directly.

### 2. V1 Desktop Integration Script (`adapters/kwin/` — KDE Plasma 6)

#### Script Implementation (`main.js`)
Hooks workspace signals, tracks hooked windows by ID, and **strictly suppresses updates when the overlay's own window is focused**:

```javascript
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

function sendState() {
    var active = workspace.activeWindow;
    
    // Invariant: If the active window is the overlay itself, DO NOT send telemetry.
    // Emitting window: null causes the avatar to flee its own input box.
    if (active && active.resourceClass === "deskmate-overlay") {
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
```

---

## Subsystem Verification & Testing

* **`test_ipc_client`:** Asserts `hello` / `welcome` handshake; verifies chunked `speech` queuing, binary PCM frame serialization, `asset_swap_result` success/failure emission, and barge-in handling.
* **`test_avatar_controller`:** Ingests audio clock ticks with viseme timeline; verifies shape key attack/decay interpolation, $\le 20\,\text{ms}$ synchrony, direct $W(t_{\text{elapsed}})$ functional evaluation without drift, and jump reset handling for discontinuities $>100\,\text{ms}$.
* **`test_asset_swap`:** Feeds corrupt `.glb`; verifies client keeps active mesh (or remains transparent on initial bootstrap) and returns `asset_swap_result: {ok: false}` without terminating.
* **`test_input_mask`:** Verifies pointer events pass through outside avatar in passive mode (`QRect(0, 0, 320, 320)`), and dynamically expand to encompass the `InteractionBar` in interactive mode (`QRect(0, 0, 320, 376)`).
* **`test_kwin_script`:** Simulates active window with `resourceClass = "deskmate-overlay"`; asserts no D-Bus dispatch is made.