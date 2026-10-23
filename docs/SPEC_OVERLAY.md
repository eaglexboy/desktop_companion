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
* **V1 Presentation Client (`overlay/`):** Implemented in **C++20 and Qt 6.8+ LTS** (`QtQuick3D`, `LayerShellQt`, `QtWebSockets`, `QtMultimedia`, plus KDE Frameworks 6 `KGlobalAccel` for global shortcuts).
* **V1 Desktop Sensing Adapter (`adapters/kwin/`):** Implemented as a **KDE Plasma 6 KWin JavaScript** automation script interacting via session D-Bus. Future platforms add sibling directories under `adapters/` (e.g. `adapters/gnome/`, `adapters/macos/`) implementing the same telemetry RPC schema.

---

## Agnostic Presentation Client Specification

### 1. Windowing, Transparency & Canvas Model
* **Non-Intrusive Presence — size comes from configuration:** The companion's size is **not a client constant**. It is the user-configurable `[presence] avatar_width_px` $\times$ `avatar_height_px` in `config.toml` (default $240\times 240\,\text{px}$; validated to $96\text{–}1024$ per side; documented in `config.example.toml`). The daemon delivers the values to the client in `welcome.config.presence`, and the client must derive from them its window size, input mask, avatar view, and interaction-bar width — never hardcode a size. The daemon uses the *same* values for corner placement, so a client that ignored them would be placed wrongly. Until `welcome` arrives the client may use the default ($240\times 240$) as a fallback. The size is read at connect time: after editing the config, restart the daemon and the overlay. The pixel figures below are the worked values for the $240\times 240$ default and scale with the configured size.
* **Wayland Layer Shell Placement:** Canvas is pinned to `LayerShellQt::Window::LayerOverlay` with an exclusion zone of `-1` (floats above applications without reserving screen work area). In QML the property is `LayerShellQt.Window.exclusionZone` (there is no `exclusiveZone`). Attached-property syntax resolves bare (`LayerShellQt.Window.layer`) only inside declarative bindings; inside imperative JS bodies (`Connections` handlers, `Timer.onTriggered`, functions) it must be qualified through an item id, e.g. `root.LayerShellQt.Window.margins`, or it throws `TypeError: Value is undefined`. `LayerShellQt::Shell::useLayerShell()` is a no-op since Qt 6.5 and is not called.
* **Wayland Output-Relative Margins:** Coordinates $(x, y)$ from `set_position` are in **global virtual-desktop space** (the same space the sensing adapter reports), while layer-shell margins are relative to whichever single output the surface is anchored to. The client therefore resolves `screen_name` against `Qt.application.screens`, moves the surface to that output, and subtracts the matched screen's `virtualX`/`virtualY` (its offset within the virtual desktop; the QML `QScreen` has no readable `geometry`) before applying the margin. Moving the surface has two constraints: (1) the LayerShellQt QML attached `screen` property cannot be assigned from QML ("Cannot assign QObject* to QScreen*", even for a typed screen), so the move goes through a small C++ helper calling `LayerShellQt::Window::setScreen()`; and (2) a layer-shell surface binds to its output when it is created, so the client must hide the window, change the screen, set the margins, and show it again — only when the output actually changes, never for same-screen moves. A failure in this path must never leave the avatar stuck faded out: an exception before the `hidden` flag is applied did exactly that. Skipping this on a multi-monitor setup places the surface entirely off-screen (e.g. `x: 4776` as a literal left margin on a 2560 px output), a failure indistinguishable from a rendering bug.
  * Moving across monitors triggers a clean fade-out ($200\,\text{ms}$) $\to$ re-anchor to the target `QScreen` $\to$ fade-in ($200\,\text{ms}$) sequence to avoid compositor re-map flicker.
* **Focus & Click-Through Policy:**
  * **Passive Mode:** The window sets `KeyboardInteractivity = None`. An input region mask (`QWindow::setMask`) is applied matching the avatar's screen bounding rectangle (`QRect(0, 0, avatar_width_px, avatar_height_px)`). Clicks outside the avatar pass through cleanly to underlying applications.
  * **Interactive Mode (`InteractionBar`):**
    * The canvas does **not** grow when the bar opens: the window and input mask stay the avatar footprint (`QRect(0, 0, avatar_width_px, avatar_height_px)`, $240\times 240$ by default). The `InteractionBar` is placed inside the empty transparent strip below the avatar's feet. The reference camera framing puts the floor line at about 77% of the view height (about 184 px down at 240 px), leaving the rest of the view unused, so the bar overlaps the lower part of the avatar view: its top is placed at $\max(0.766\,H + 8,\; H - 48 - 12)$ for view height $H$ — $192\,\text{px}$ at the 240 px default, i.e. about $8\,\text{px}$ under the feet and flush with the bottom of the window. Stacking it below the whole view (an earlier $8\,\text{px}$-gap, 376 px-tall layout) left a large dead gap and, when the avatar sat low on screen, pushed the bar off the bottom of the output.
    * Layout metrics: width equal to the avatar width, height $48\,\text{px}$ (not scaled, to stay readable). Controls include a single-line text input field that takes the remaining width and a Push-to-Talk button ($36\times 36\,\text{px}$). The bar must stay readable over arbitrary (often dark) windows: opaque fill with a light, $2\,\text{px}$ border rather than a dark translucent one.
    * Because the bar sits in the strip below the floor line, a body shorter or taller than the reference does not move it (the floor line is fixed at the origin); the placement constant must be revisited if camera framing changes.
    * The window switches to `KeyboardInteractivity = OnDemand` and acquires keyboard focus.
  * **Dismissal:** Dismissed via `Escape` key, clicking outside the window (detected via `focusOutEvent`), or pressing global shortcut `toggle_interaction`. On dismissal, the interaction bar collapses and the window returns to `KeyboardInteractivity = None`.
  * **Focus-loss debounce:** Opening the bar changes keyboard interactivity, and the compositor briefly drops and regains focus while doing so. Dismissal on focus loss must therefore wait a short settle delay (V1: 250 ms) and act only if focus is still lost; reacting to the first loss closes the bar the instant it opens.
* **System Tray Icon (manual recovery):** The client registers a StatusNotifier tray icon (V1: `Qt.labs.platform` `SystemTrayIcon`, theme icon `face-smile`) so the user can always bring the companion back and does not depend on its on-screen presence. Activating the icon (click) and the menu item *Show companion* run `showCompanion()`: clear the `hidden` flag, force a re-home to the output, re-apply the last daemon-given position, and recreate the surface if it is not visible. The menu also has *Talk to companion…* (shows the companion, then toggles the input bar) and *Quit*. Quit exits with status **3**, which the service's restart policy excludes, so a deliberate quit sticks while every other exit is restarted. Implementation constraints: the tray icon's menu is widget-based, so the client must use `QApplication` (not `QGuiApplication`) and link `Qt6::Widgets`, or it aborts at startup with "Cannot create a QWidget without QApplication"; the QML module `Qt.labs.platform` is a runtime dependency (Debian/Ubuntu `qml6-module-qt-labs-platform`).
* **Display Loss & Recovery (screen lock / DPMS / hot-unplug):** When every output disappears the compositor destroys layer-shell surfaces, Qt logs `There are no outputs - creating placeholder screen`, and the windows close. The client must **not** quit when its last window closes (`quitOnLastWindowClosed = false`): doing so exits cleanly, a restart-on-failure policy does not revive a clean exit, and the companion silently never returns after unlock. When an output is added again (debounced ~600 ms, since outputs appear one at a time) the client runs the same `showCompanion()` recovery as the tray icon, using its remembered last position. The service unit additionally uses `Restart=always` with `RestartPreventExitStatus=3` as a backstop for crashes.
* **Fullscreen Evasion & Hiding:** When receiving `hidden: true`, the client fades out cleanly; it restores visibility upon receiving `hidden: false`. The fade is applied to an ordinary child `Item` that wraps the avatar and interaction-bar content, **not** to the top-level `Window`: the Wayland layer-shell QPA plugin does not support `QWindow::setOpacity()`, whereas Quick Item opacity is a scene-graph concern that always works.

### 2. Locomotion & Non-Blocking Repositioning
* Same-screen moves must interpolate position over `duration_ms` with ease-out curves, by animating the `LayerShellQt.Window.margins` left/top with a `NumberAnimation` (reachable through the qualified attached property). Cross-screen moves use the fade-out / re-anchor / fade-in sequence above.
* **Implemented:** the surface's output-local position lives in `slideX`/`slideY` properties that the margins are bound to; a same-output `set_position` runs a `ParallelAnimation` of both over `duration_ms` (InOutQuad). The first placement, a zero duration and any cross-output move jump instead (a layer-shell surface cannot straddle two outputs). The speech bubble follows the avatar on every animation frame, so it rides along while the avatar runs.
* **Evasion Motion (`move`):** While translating across coordinates ($\Delta(x, y) > 0$), the client triggers the `move` animation action for the duration of the translation, transitioning back into the active idle upon arrival. Baked clips are stripped at load (`GlbSanitizer`), so in practice `IdleBehaviors.qml` always uses the procedural path: while `moving` it hops (a looping `position.y` bounce) and leans into the direction of travel (`eulerRotation.z`, skipped while thinking), then settles back to rest. If the active model lacks a `move` clip, it falls back to a procedural lean/squash in the direction of travel.

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
  * V1 limitation: the client applies a fixed ~4.5 Hz pulse gated on playback state (active only while audio plays). `QMediaPlayer` exposes no amplitude tap, so true RMS-driven modulation requires a lower-level `QAudioSink`-based playback pipeline, which is substantially more work than V1's scope allowed.
* **Chunk Queueing:** `speech` arrives as per-sentence chunks (`chunk_seq`, `final`) sent as each finishes synthesizing. The client queues chunks and plays them back-to-back, loading each chunk's viseme timeline when that chunk starts. A chunk with `chunk_seq == 0` begins a new utterance and preempts any queued or playing speech. If a non-final chunk finishes before the next one arrives, the client stays armed and resumes when it does. `speech_finished` is emitted only after the `final` chunk has finished playing.
* **Speech Bubble (`[presence] show_speech_bubble`, default on):** While the companion speaks, the client shows the text being said in a bubble beside the avatar. This exists because speech has unavoidable latency — the first words can be missed and there can be a pause between sentences — so the user can read what they missed. Behavior:
  * The bubble shows the sentences of the current reply spoken *so far* (each chunk's `text`, appended when that chunk starts playing), trimmed to the most recent sentences that fit about 240 characters, so a missed first sentence stays readable while the reply continues. A `chunk_seq == 0` chunk starts a fresh bubble.
  * After the `final` chunk finishes it lingers (V1: 3.5 s) and then clears. Barge-in clears it immediately. It is hidden while the avatar itself is hidden.
  * It is a **separate, click-through layer-shell surface**, not part of the avatar window: the avatar window must remain exactly the avatar footprint (daemon placement, the daemon's own-window rejection, and the input bar all depend on that), and a bubble needs room above or below the avatar. It is centered on the avatar, placed above it when it fits and below it otherwise (avatar near the top of the screen), clamped to the output, and follows the avatar across outputs using the same hide / re-home / show sequence (a `suspended` flag rather than assigning `visible`, which would break the visibility binding). A layer-shell surface cannot have a transient parent, so it must set `transientParent: null`.
  * **Re-homing after display loss:** the compositor destroys layer-shell surfaces when outputs disappear (screen lock, wake, hot-unplug), and Qt may silently move them to another output. The avatar and the bubble each remember which output they are anchored to and skip re-homing when it already matches, so both must forget it in `showCompanion()` (called by `recoverDisplay()` and the tray). If the bubble's memory were left stale it would stay on the wrong monitor while the avatar returned to the right one.
  * Click-through uses a 1×1 input region, because an *empty* mask means "no mask" to Qt and the whole bubble would swallow clicks; the mask is re-applied every time the surface is re-created.
  * **Voice level:** `[voice.tts] volume` (0.0–1.0, linear) arrives in `welcome.config.voice.volume` and again in any `config_update`; the client sets its `AudioOutput.volume` from it (clamped; a missing or non-numeric value leaves the current level alone). Changing it takes effect live, no restart.
  * When `show_speech_bubble = false` the client never creates a visible bubble. The setting arrives in `welcome.config.presence` (restart the overlay after changing it).
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

* **V1 Reference Implementation:** In native KDE Plasma 6 desktop deployments, `shortcut_manager.cpp` registers global hotkeys through the KF6 `KGlobalAccel` C++ API (`KGlobalAccel::setShortcut()` and the `globalShortcutActiveChanged` signal), which talks to KDE's `org.kde.kglobalaccel` service for us — hand-rolled D-Bus calls would depend on undocumented flag-bit semantics. The client registers a `deskmate-overlay` component with `toggle_interaction` and `push_to_talk` actions. The XDG Desktop Portal is reserved as a fallback for sandboxed builds.
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
         "presence": {"avatar_width_px": 240, "avatar_height_px": 240, "show_speech_bubble": true},
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
`CMakeLists.txt` finds and links these Qt 6 and KDE Frameworks components: `Core`, `Gui`, `Qml`, `Quick`, `Quick3D`, `Quick3DAssetUtils` (which transitively requires the `QuickTimeline` CMake config, packaged separately from its runtime QML module), `QuickControls2`, `WebSockets`, `Multimedia`, `Test`, `LayerShellQt` (CMake package `LayerShellQt`, target `LayerShellQt::Interface` — not `LayerShellQtInterface`), and `KF6GlobalAccel`. `target_include_directories(deskmate_overlay PRIVATE src)` is required: `qt_add_qml_module`'s generated registration code includes each `QML_ELEMENT` header with angle brackets, which resolve only through the include search path.

`tinygltf` must remain declared via `FetchContent` as a ready fallback. V1 does not link it into any target, because inital rendering spike found no morph or blending defects in Qt's `RuntimeLoader` on the reference stack; link it (one `target_link_libraries` line) if a different driver stack fails the spike.

#### Qt 6 API Constraints
* `RuntimeLoader` is in the `QtQuick3D.AssetUtils` QML module (not `QtQuick3D.Helpers`).
* `QQuick3DNode`, `QQuick3DModel`, `QQuick3DMorphTarget`, and `RuntimeLoader` are QML-only types backed by private headers; they are not public C++ API. `AvatarController::root` is therefore a plain `QObject*`, and morph weights are applied with `target->setProperty("weight", value)` rather than a `qobject_cast` to a type the C++ code may not name.
* `PerspectiveCamera` must set `clipNear` explicitly (e.g. `0.1`): the default silently near-plane-culls meter-scale content at the close camera distance needed to frame a ~2.5 m character in a small square view (e.g. the 240×240 default), producing an empty scene even though the model loaded successfully.
* **`RuntimeLoader` auto-plays every imported glTF animation and exposes no way to stop it** (Qt 6.10). The generated avatars bake about 20 head-bone clips that all drive the same bone, so loading one verbatim makes the head thrash between them (a glitchy, looping bob that clips into the body). The client never plays baked clips — idle and gesture motion are QML transform animations — so it must give `RuntimeLoader` a copy of the file with the glTF top-level `animations` array removed (all meshes, the skin, and morph targets preserved byte for byte; `GlbSanitizer` in `overlay/src/glb_sanitizer.*`, cached by content hash). A file that cannot be parsed is passed through unchanged so the normal load-failure path (and `asset_swap_result: {ok: false}`) still applies. The baked clips remain in the asset for other clients and tools.
* **Imported morph targets start at weight 1.0 and have no `objectName`.** Left alone, all nine mouth shapes are fully on at once from load (a wide black bar for a mouth), and any lookup by shape-key name can never match (so lip-sync moves nothing). The client must (1) drive every morph weight to 0 after every load — including hot reloads, which reuse the same loader object, and with retries, because the loader keeps instantiating scene objects after its status reaches `Success` — and (2) map targets by index: the i-th morph target of a model is the i-th entry of the manifest's `viseme_shape_keys`, which the client therefore takes from `welcome`/`hot_reload_asset` before loading the file.
* `RuntimeLoader.bounds` reads `{0,0,0}-{0,0,0}` when sampled synchronously in `onStatusChanged` even for a successful load; client logic must not depend on it.

#### Asset Failure & Malformed Binary Invariant
If a loaded asset fails parsing or exhibits invalid glTF magic bytes, the client remains running in a zero-opacity transparent state, preserves its active WebSocket connection to the daemon, and emits `asset_swap_result: {"ok": false, "version": N, "error": "<reason>"}` without crashing.

#### Phase-0 Rendering De-Risking Spike
1. Loads an exported test `.glb` containing skeleton, 9 canonical Rhubarb shape keys, and 3 animation clips.
2. Asserts morph target indexing and dynamic weight assignment from C++ at 60 fps.
3. Tests animation clip blending across base and cognitive tiers.
4. *Fallback Guarantee:* If Qt's `RuntimeLoader` morph/blending APIs exhibit driver-level defects on a given driver stack, `avatar_controller.cpp` must engage the vendored `tinygltf` parser to construct `QQuick3DGeometry` directly. V1 does not implement this path because the reference stack passed the spike.

### 2. V1 Desktop Integration Script (`adapters/kwin/` — KDE Plasma 6)

#### Packaging & Behavior Notes
* `metadata.json` follows the Plasma 6 KWin-script schema: `KPackageStructure: "KWin/Script"`, a nested `KPlugin` object (`Id`, `Name`, `Description`, `Authors`, `License`, `Icon`, `Version`), and `X-Plasma-API: "javascript"`. No `X-Plasma-MainScript` key is needed; `contents/code/main.js` is the fixed-by-convention entry point. It is installed with `kpackagetool6 --type KWin/Script` and enabled via `kwriteconfig6 --file kwinrc --group Plugins --key deskmate-presenceEnabled true` followed by an `org.kde.KWin.reconfigure` call.
* `callDBus` to `org.deskmate.Presence` fails silently when nothing owns that name (the normal state while the daemon is stopped), logging neither errors nor warnings, so the script is safe to leave permanently installed and enabled.
* The `QTimer` single-shot-replace pattern below genuinely debounces in KWin's JS engine: rapid-fire triggers ~20 ms apart collapse into one `UpdateState` call.
* Self-suppression by name is best-effort: the exact class a layer-shell surface reports could not be confirmed (an earlier exact `resourceClass === "deskmate-overlay"` check did not hold while the input bar had focus, and the avatar flapped between corners). The daemon-side geometry guard is the dependable layer; the name check just avoids sending the redundant update.

#### Script Implementation (`main.js`)
Hooks workspace signals, tracks hooked windows by ID, and **suppresses updates when the overlay's own window is focused**. The name check compares class/name/desktop-file identifiers case-insensitively with `_` treated as `-` (the binary is `deskmate_overlay`, the app name `deskmate-overlay`, and which one a layer-shell surface reports is not guaranteed); it never consults the window title, because unrelated windows legitimately mention "Deskmate" in theirs. This check is a first line of defense only — the Brain daemon independently discards telemetry matching the avatar's own last-placed rectangle (see `docs/SPEC_BRAIN.md`, Own-window rejection), so a miss here cannot make the avatar flee its own window:

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
    if (active && isOverlayWindow(active)) {
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
* **`test_viseme_math` (automated Qt Test):** The pure envelope math — shape key attack/decay interpolation, direct $W(t_{\text{elapsed}})$ functional evaluation without drift, and jump reset handling for discontinuities $>100\,\text{ms}$.
* **`test_window_mask_helper` (automated Qt Test):** Exercises real `QWindow::setMask()` against an offscreen `QQuickWindow`, verifying the helper applies exactly the rectangle it is given.
* **`test_ipc_client` (automated Qt Test):** Runs the full `hello`/`welcome` handshake and every wire event against a real `QWebSocketServer` standing in for the daemon.
* **Live verification (not automated):** End-to-end avatar behavior — real asset load via `RuntimeLoader`, morph-target binding, the `asset_swap_result: {ok: false}` corrupt-`.glb` path, and ≤ 20 ms synchrony — requires a real `QQuick3DNode` scene graph with loaded morph targets, which a lightweight offscreen Qt Test binary cannot cheaply stand up. These are verified against a running daemon and real desktop screenshots instead (`spectacle` via `org.kde.KWin.ScreenShot2`), and window placement against a KWin-script dump of `workspace.windowList()`. Testing hierarchy for rendering bugs: first confirm the window exists at the right position and size (KWin dump), then confirm opaque solid colors composite, and only then investigate 3D content.
* **`test_kwin_script`:** Simulates active window with `resourceClass = "deskmate-overlay"`; asserts no D-Bus dispatch is made.