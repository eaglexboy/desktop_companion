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
* **Faceless Mode (`has_face: false`):** Plays the baked `talk_pulse` action and modulates the emissive shader channel in real time based on audio RMS energy.
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