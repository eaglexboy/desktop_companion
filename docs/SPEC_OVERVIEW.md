# Deskmate — Specification Overview

## Project Summary

Deskmate is an interactive, on-screen desktop companion agent designed for Linux desktop environments (KDE Plasma 6 on Wayland). The agent lives on screen, avoids obstructing active windows (dynamically repositioning or migrating to secondary displays), routes conversations between local and cloud language models, features a procedurally generated 3D visual avatar with real-time lip synchronization, and evolves a persistent personality over time.

The project is built as a reusable, modular system where all machine-specific paths, credentials, and configurations are abstracted into user-editable configuration files.

## Scope: V1 and Beyond

* **Desktop Environment Integration:** Scoped in V1 to Linux with **KDE Plasma 6 on Wayland** via the KWin scripting API, packaged as the `adapters/kwin/` sensing adapter, and the session D-Bus bridge (`org.deskmate.Presence`). Contracts are engine-agnostic so alternate environments (GNOME, Hyprland, macOS) can be introduced in future versions as additional sibling adapters under `adapters/`, without modifying the core daemon.

* **Frontend Presentation Layer:** The presentation client is decoupled via local WebSocket IPC. In V1, the reference client is implemented in **C++20 and Qt 6.8+ LTS** (`QtQuick3D`, `LayerShellQt`). Alternate renderers (Godot, Tauri) can connect using the same protocol.

* **Network & Mobile Multi-Client Access (V2+ Roadmap):** V1 binds strictly to the local loopback interface (`127.0.0.1:8765`), secured against browser-origin hijacking. The protocol is designed to be "Network-Ready" (supporting dual local paths and HTTP URLs). Private mesh networking (Tailscale) and mobile PWA clients are detailed in `docs/ADR_NETWORK_AND_MOBILE_STRATEGY.md`.

* **Autonomous Voice Selection & Cloning (V2+ Roadmap):** In V1, the user configures the voice model, while the daemon supports per-call voice overrides. Autonomous acoustic evolution (allowing the companion to evolve its voice alongside its appearance) and voice cloning safeguards are detailed in `docs/ADR_AUTONOMOUS_VOICE_SELECTION.md`.

* **Skills & Sub-Agent Orchestration (V2+ Roadmap):** Deterministic script DAG execution and cognitive multi-turn sub-agents governed by the Inference Capacity Governor (ICG) are detailed in `docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md`.

* **Configuration Interface:** V1 relies on direct editing of `$XDG_CONFIG_HOME/deskmate/config.toml` with live zero-downtime hot-reloading. Dedicated graphical settings interfaces are deferred to V2+.

## Architectural Components

| Component | Target Role | Implementation Path | 
| ----- | ----- | ----- | 
| **Brain Daemon (Python)** | Core orchestration: configuration watcher, dual-tier LLM routing, personality evolution & reflection, pluggable STT/TTS, Blender asset orchestration, window presence algorithms, and WebSocket IPC hub. | `brain/` (Details: `docs/SPEC_BRAIN.md`) | 
| **Parametric Asset Generator (Blender)** | Headless procedural asset pipeline: compiles declarative `LookSpec` definitions into fully rigged, viseme-capable, and animated `.glb` 3D character models for bootstrap and runtime redesigns. Targets **Blender 5.0.1** (verified host target; **4.5 LTS+ compatibility floor**). | `blender/` (Details: `docs/SPEC_BLENDER.md`, `docs/DESIGN_AVATAR_EVOLUTION_FLOW.md`) | 
| **Desktop Overlay Application (Qt 6 / C++20)** | Transparent Wayland rendering client (`LayerShellQt`, `QtQuick3D`): handles interactive text/mic input, `.glb` hot-reloading, local audio clock lip-sync, and non-blocking evasion. | `overlay/` (Details: `docs/SPEC_OVERLAY.md`) | 
| **Desktop Sensing Adapters (per-platform)** | Platform-specific adapters registering workspace and window geometry callbacks, debouncing updates, filtering out the overlay's own window, and dispatching telemetry over D-Bus. V1 ships the KWin (KDE Plasma 6) adapter; future adapters for other desktop environments/OSes live alongside it. | `adapters/` (V1: `adapters/kwin/`) | 
| **Systemd Units & Deployment Scripts** | User-level process supervision, environment bootstrapping, dependency synchronization, and automated background service management. | `deploy/` | 

## Architecture Diagram

```
┌─────────────────────────────┐        Local WebSocket IPC (JSON envelopes) ┌─────────────────────────────┐
│  overlay/                   │◄──────────────────────────────────────────► │  brain/  (Python daemon)    │
│  Frontend rendering client  │   Position cmds, speech + visemes, inputs   │  Router, personality,       │
│  (V1: C++ / QML on Wayland) │───────────────────────────────────────────► │  voice, appearance, presence│
└─────────────▲───────────────┘                                             └─────┬───────────┬───────────┘
              │ Window geometry updates                             OpenAI-compat │           │  Cloud LLM
              │ (excluding overlay)                                  /v1 endpoint │           │  APIs
┌─────────────┴───────────────┐                                                   ▼           ▼
│ adapters/kwin/ (JS in KWin) │                                             Local LLM      Cloud LLM(s)
│ Desktop sensing adapter     │  D-Bus (Session Bus: org.deskmate.Presence) (Fast tier)   (Complex tier)
│ Debounced active windows,   │─────────► brain/presence/desktop_bridge.py
│ screens, and desktop state  │
└─────────────────────────────┘

blender/generator.py  ◄── Invoked headless by brain/appearance/blender_gen.py ── Consumes LookSpec
   │                      (Triggered at setup or on autonomous redesign per DESIGN_AVATAR_EVOLUTION_FLOW.md)
   └─ Exports .glb (Mesh + Armature + Rhubarb Visemes + State Clips)
         │
         ▼
data/current/  ──────── Hot-reloaded by overlay via IPC notification ────────►  Runtime asset store

```

## Target Repository Layout

```
deskmate/
├── brain/                    # Python core daemon
│   ├── config.py             # Configuration schema, loader, and live reload watcher
│   ├── llm_pool.py           # Multi-provider client abstraction (plugin adapter per provider)
│   ├── router.py             # Routes by complexity (Local vs. Cloud) and capability tags
│   ├── main.py               # Daemon entry point, FastAPI lifecycle, and background loops
│   ├── personality/
│   │   ├── state.py          # SQLite schema (traits, mood, identity, memories, interactions, agent_state)
│   │   ├── reflect.py        # Dialogue reflection loop, delta proposals, cooldown checks
│   │   └── prompts.py        # Dynamic prompt synthesis from traits, mood, and context
│   ├── voice/
│   │   ├── stt.py            # STT coordinator, audio chunk buffering, silence endpointing
│   │   ├── stt_local.py      # Embedded offline STT runner (faster-whisper)
│   │   ├── stt_remote.py     # Remote HTTP STT client
│   │   ├── tts.py            # Pluggable TTS interface with per-call voice overrides
│   │   ├── tts_local.py      # Embedded local TTS backend (Piper CLI)
│   │   ├── tts_remote.py     # Remote HTTP TTS client (tts-serve)
│   │   └── lipsync.py        # Audio waveform analysis to time-indexed viseme sequences (rhubarb CLI + viseme_shapekeys.py)
│   ├── appearance/
│   │   ├── spec.py           # Declarative LookSpec schema validation against catalogs
│   │   ├── blender_gen.py    # Headless Blender orchestration, environment sanitization
│   │   └── manifest.py       # AssetManifest schema, versioning, and disk rotation
│   ├── presence/
│   │   ├── desktop_bridge.py # Generic D-Bus service receiving desktop telemetry
│   │   └── placement.py      # Screen occupancy and evasion geometry algorithms
│   ├── ipc/
│   │   └── server.py         # Async WebSocket hub with Origin validation and handshake
│   └── tests/                # Automated unit and integration test suite
│
├── blender/                  # Parametric asset generation scripts
│   ├── generator.py          # Parametric geometry, rigging, wardrobe, and animation generator
│   └── viseme_shapekeys.py   # Canonical Rhubarb phoneme-to-viseme target mappings (pure Python)
│
├── overlay/                  # Frontend rendering client (V1 reference implementation)
│   ├── CMakeLists.txt        # Build system (Qt 6.8+, Quick3D, WebSockets, LayerShellQt, tinygltf)
│   ├── src/
│   │   ├── main.cpp          # Client runtime entry point
│   │   ├── ipc_client.h / .cpp       # QWebSocket client wrapping JSON envelope protocol
│   │   ├── avatar_controller.h / .cpp# Scene graph traversal, morphs, animation blending
│   │   ├── audio_capture.h / .cpp    # QtMultimedia microphone chunker (16kHz PCM16LE binary frames)
│   │   └── shortcut_manager.h / .cpp # KDE KGlobalAccel D-Bus registration
│   └── qml/
│       ├── Main.qml          # LayerShell transparent window surface
│       ├── Avatar3D.qml      # QtQuick3D View3D, RuntimeLoader, directional lighting
│       ├── InteractionBar.qml# Collapsible text input field & mic push-to-talk button
│       └── IdleBehaviors.qml # Procedural micro-movements, think loop, and mood blending
│
├── adapters/                  # Desktop sensing adapters, one per platform
│   └── kwin/                  # V1 reference adapter (KDE Plasma 6 / KWin backend)
│       ├── metadata.json      # KWin script registration manifest
│       └── contents/code/main.js # Debounced workspace listener dispatching D-Bus events
│
├── deploy/                   # Installation and service definitions
│   ├── install.sh            # Dependency installer and environment bootstrapper
│   ├── sync_pip.sh           # Environment sync utilities
│   └── systemd/              # User-level service units
│       ├── deskmate-brain.service
│       └── deskmate-overlay.service
│
├── docs/                     # Design documentation & architectural decision records
│   ├── SPEC_BRAIN.md
│   ├── SPEC_BLENDER.md
│   ├── SPEC_OVERLAY.md
│   ├── DESIGN_AVATAR_EVOLUTION_FLOW.md
│   ├── ADR_NETWORK_AND_MOBILE_STRATEGY.md
│   ├── ADR_AUTONOMOUS_VOICE_SELECTION.md
│   └── ADR_SKILLS_AND_AGENT_ORCHESTRATION.md
│
├── examples/
│   └── sample_look.json      # Reference LookSpec definition
│
├── config.example.toml       # Documented configuration template
├── pyproject.toml            # Python packaging and dependency declarations
└── README.md                 # System overview and getting started guide
```

## Dependency Manifest & Packaging (`pyproject.toml`)

The Python Brain daemon manages dependencies via standard packaging tools (`pyproject.toml`). The pinned dependency tiers include:

### 1. Python Runtime Dependencies

```toml
[project]
name = "deskmate-brain"
version = "0.1.0"
description = "Brain daemon and intelligence orchestration engine for the Deskmate desktop companion."
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    # Core Server & IPC Transport
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "websockets>=12.0",
    "pydantic>=2.8.0",
    "watchdog>=4.0.0",

    # Asynchronous Networking & LLM Clients
    "httpx>=0.27.0",

    # Linux Desktop D-Bus Bridge
    "jeepney>=0.8.0",

    # Audio Ingestion & Local Speech-to-Text
    "faster-whisper>=1.0.0",
    "soundfile>=0.12.0",
    "numpy>=1.26.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "ruff>=0.5.0",
    "mypy>=1.10.0"
]
```

### 2. External System Binaries & Compatibility Baseline

The system requires the following external binaries discoverable on `PATH` or configured via `config.toml` (installed by `deploy/install.sh`):

| Dependency | Purpose | Target Version / Baseline |
|---|---|---|
| **Blender** | Headless 3D mesh compilation & asset inspection (`--inspect`). | Target: **5.0.1**; Compatibility Floor: **4.5 LTS+** |
| **Piper TTS** | Fast, local offline speech synthesis (`piper` CLI). | $\ge 1.2.0$ |
| **Rhubarb Lip Sync** | Audio-to-phoneme waveform analysis invoked headlessly by `lipsync.py` (`rhubarb` CLI). | $\ge 1.13.0$ |
| **Bubblewrap** | Unprivileged Linux sandbox isolation for Track 1 script tasks and skills (`bwrap` CLI). | $\ge 0.8.0$ (standard Linux desktop package) |

## Technical Specifications & Interfaces

### 1. Brain Daemon (`brain/`)

* **Inference Routing:** Evaluates incoming user inputs to route requests either to a local OpenAI-compatible endpoint or escalates to Cloud LLMs based on task affinities (`code`, `complex_reasoning`, `large_context`).
* **Personality & Memory:** Periodic reflection loop (`reflect.py`) maintains semantic memories, logs interactions, decays volatile moods, and evaluates autonomous appearance redesigns subject to personality gates and a 12–24 hour cooldown window.
* **Speech & Local Clock Lip Sync:** Buffers microphone chunks, transcribes via pluggable STT (`stt.py`), synthesizes speech via local or remote TTS, extracts canonical visemes using the `rhubarb` CLI mapped through `viseme_shapekeys.py`, and packages the complete timeline into the `speech` event.
* **Presence Monitoring:** Receives debounced telemetry via `desktop_bridge.py` (`org.deskmate.Presence`). Calculates non-overlapping screen coordinates (`placement.py`) and issues `set_position`.

### 2. WebSocket IPC Protocol

* **Handshake:** Client emits `hello` declaring client ID and capabilities; daemon replies with `welcome` containing asset directories, active manifest, and configurations.
* **Core Inbound Events (Daemon $\to$ Client):** `welcome`, `chat_response`, `set_position`, `speech` (with audio source, delivery style, and complete viseme array), `hot_reload_asset`, `status_update`, `mood_update`, `play_gesture`, `config_update`.
* **Core Outbound Events (Client $\to$ Daemon):** `hello`, `user_text_input`, `audio_stream_start`, `audio_stream_chunk`, `audio_stream_end`, `asset_swap_result`, `speech_finished`, `interaction_state`.

### 3. Parametric Generator (`blender/`)

* Runs headlessly via `blender --background --python blender/generator.py -- <args>`. Supports `--spec <path> --out <path>` for compilation and `--inspect <path> --out <path>` for custom template discovery.
* Follows the **3-Tier Asset Resolution Hierarchy**: Tier 1 (user custom templates in `assets/custom_templates/`), Tier 2 (standard library templates), and Tier 3 (pure mathematical procedural primitives).
* **Hybrid Wardrobe Pipeline:** Fits body garments using procedural mesh extraction and solidification directly from base topology (guaranteeing zero clipping across any body scale) while supporting modular socket-attached mesh accessories.
* Bakes base idles, personality mood idles (`idle_bored`, `idle_curious`, etc.), conversational delivery gestures (`talk_subdued`, `talk_animated`, etc.), and state loops (`move`, `think`).

### 4. Overlay Client (`overlay/`)

* Transparent, borderless Wayland surface managed via `LayerShellQt`. Sized by default to `avatar_width_px` $\times$ `avatar_height_px`.
* Pinned to `LayerShellQt::Window::LayerOverlay` with `exclusiveZone = -1`.
* Operates in passive click-through mode (`KeyboardInteractivity = None`) using a screen-space input mask; switches to interactive mode (`KeyboardInteractivity = OnDemand`) when the interaction bar is summoned.
* Evaluates viseme weights directly against the local `QMediaPlayer` audio clock, eliminating network jitter from facial animation.
* Preloads and validates new assets in the background upon receiving `hot_reload_asset`, returning `asset_swap_result` to confirm or trigger rollbacks.

### 5. Desktop Sensing Adapter — V1 Reference (`adapters/kwin/`)

* Hooks KWin workspace signals (`windowActivated`, `frameGeometryChanged`, `screensChanged`).
* Debounces geometry dispatches by 100ms and explicitly ignores the overlay's own window (`resourceClass !== "deskmate-overlay"`).
* Dispatches active window bounding boxes and monitor geometries over session D-Bus (`org.deskmate.Presence`).

## Configuration Architecture (`config.toml`)

Operational settings are loaded from `$XDG_CONFIG_HOME/deskmate/config.toml` with live file watching:

* `[general]`: Logging verbosity and output routing.
* `[server]`: Host interface, port (`8765`), and `allowed_origins` for WebSocket security.
* `[storage]`: Scratch path (`workspace_dir`) and persistent path (`deploy_dir`).
* `[llm.local]`, `[llm.cloud.<provider>]`, `[llm.routing]`: Endpoints, models, timeouts, and capability tags.
* `[voice.stt]`, `[voice.tts]`: Backend selections (`local` vs. `remote`), models, endpoints, and dynamic parameters.
* `[presence]`: Screen margins, display affinity, and avatar pixel dimensions.
* `[shortcuts]`: Global keybindings and `push_to_talk_mode = "hold" | "toggle"`.
* `[personality]`: Identity name, trait lists, mood axes, and reflection/decay intervals.
* `[appearance]`: Blender binary path, default LookSpec, retention count, and `redesign_cooldown_hours_min/max` (12–24h).

## System Integration & Verification Strategy

### 1. The System "Golden Path" Smoke Test

1. **Daemon Initialization:** Launch `brain/main.py`. Verify `GET /health` returns `200 OK` with active traits and voice.
2. **Asset Bootstrap:** Run headless Blender generation to produce initial `look-v1.glb` and `manifest.json`.
3. **Overlay Handshake:** Launch `overlay`; verify `hello` $\to$ `welcome` handshake and initial `set_position`.
4. **Synthetic Interaction Loop:**
   * Transmit a `user_text_input` event over WebSocket.
   * Verify the daemon routes dialogue, returns `chat_response`, synthesizes speech, and emits `speech` with visemes.
   * Verify the overlay plays audio and synchronizes morph targets to the audio clock.

### 2. Cross-Component Contract Verification

* **IPC Protocol Contract:** Automated tests assert that messages emitted by daemon and overlay strictly conform to shared JSON Schemas.
* **D-Bus Telemetry Contract:** Automated tests verify `desktop_bridge.py` correctly parses debounced payloads from `main.js`.
* **Asset Metadata Contract:** Automated tests assert that shape key names and animation clips in `.glb` files match `viseme_shapekeys.py` and are accurately recorded in `manifest.json`.

### 3. System Degradation Matrix

| Failure Scenario | Subsystem Handling | System Behavior | 
| ----- | ----- | ----- | 
| **Daemon restarts** | Overlay enters exponential reconnection backoff. | Avatar idles safely without freezing; re-handshakes and syncs state upon reconnect. | 
| **KWin / D-Bus absent** | `desktop_bridge.py` catches missing session bus. | Daemon defaults avatar placement to a safe margin corner on the primary display. | 
| **Cloud LLM drops** | `router.py` catches timeout / 5xx error. | Silently fails over to the local model tier; returns response with operational log. | 
| **Remote TTS / STT drops** | Voice manager catches connection failure. | Automatically redirects synthesis/transcription to local engines (`piper` / `faster-whisper`). | 
| **Blender build failure** | `blender_gen.py` encounters build error. | Retains current active `.glb` asset; logs failure without interrupting chat or presence loops. | 
| **Overlay swap failure** | Client reports `asset_swap_result: {ok: false}`. | Daemon rolls `manifest.json` back to previous version; companion informs user gracefully. | 