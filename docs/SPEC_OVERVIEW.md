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
| **Status Page (Python, standalone)** | A separate, read-only process serving a browser dashboard: service/endpoint health, live pipeline view (router decisions, model reasoning, skill calls), mood/trait timeline, chat history and logs. Separate from the brain so it stays up when the brain does not; it never writes state. Binds `127.0.0.1:8780`. | `status_page/` (Details: `docs/SPEC_BRAIN.md`, "Event Stream"; `docs/SPEC_STATUS_PAGE.md`) |
| **Systemd Units & Deployment Scripts** | User-level process supervision, environment bootstrapping, dependency synchronization, scheduled state backup, and automated background service management. `install.sh` installs all five systemd units (brain, overlay, status, backup service and timer), substituting `@REPO_ROOT@` and `@VENV_PYTHON@` (resolved from `storage.workspace_dir`, since the venv does not live in the repo checkout), seeds config and catalogs idempotently, and installs the KWin adapter (upgrading in place if present). | `deploy/` | 

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
│   ├── evolution.py          # AvatarEvolution glue: appearance tools, staged reveal, presence-driven reveal, autonomous redesign gates
│   ├── main.py               # Daemon entry point, FastAPI lifecycle, administrative REST surface, and background loops
│   ├── events.py             # In-process event bus (bounded, non-blocking) behind GET /api/events
│   ├── skills/               # Structured replies and the allowlisted actions the companion can perform
│   │   ├── registry.py       # Skill/Param/SkillRegistry: validation, per-reply cap, timeouts
│   │   ├── reply.py          # Splits a model reply into dialogue ("say") and actions ("do")
│   │   └── builtin.py        # gesture, remember, change_look, revert_look
│   ├── personality/
│   │   ├── state.py          # SQLite schema (traits, mood, identity, memories, interactions, agent_state)
│   │   ├── reflect.py        # Dialogue reflection loop, delta proposals, cooldown checks
│   │   └── prompts.py        # Dynamic prompt synthesis from traits, mood, and context
│   ├── voice/
│   │   ├── plaintext.py      # Strips Markdown/emoji from replies; maps vocal cues (*sigh*) to sounds
│   │   ├── stt.py            # STT coordinator, audio chunk buffering, silence endpointing
│   │   ├── stt_local.py      # Embedded offline STT runner (faster-whisper)
│   │   ├── stt_remote.py     # Remote HTTP STT client
│   │   ├── delivery.py       # Deterministic traits/mood -> delivery style mapping (no TTS dependency)
│   │   ├── tts.py            # Pluggable TTS interface, priority pool, per-call voice overrides
│   │   ├── tts_local.py      # Embedded local TTS backend (Piper CLI)
│   │   ├── tts_remote.py     # Remote HTTP TTS clients, provider_type registry (tts_serve, generic_openai)
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
│   ├── CMakeLists.txt        # Build system (Qt 6.8+, Quick3D, WebSockets, LayerShellQt, KF6GlobalAccel; tinygltf declared, unlinked)
│   ├── src/
│   │   ├── main.cpp          # Client runtime entry point
│   │   ├── ipc_client.h / .cpp       # QWebSocket client wrapping JSON envelope protocol
│   │   ├── avatar_controller.h / .cpp# Scene graph traversal, morphs, animation blending
│   │   ├── audio_capture.h / .cpp    # QtMultimedia microphone chunker (16kHz PCM16LE binary frames)
│   │   └── shortcut_manager.h / .cpp # Global shortcut registration via the KF6 KGlobalAccel API
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
│   ├── sync_pip.sh           # Builds/updates the venv in workspace_dir; optional mirror via DESKMATE_VENV_MIRROR_DIR
│   ├── backup_state.sh       # Cold mirror of deploy_dir to [storage] backup_dir (state.db via SQLite online backup + integrity check, rest via rsync)
│   └── systemd/              # User-level service units (placeholders @REPO_ROOT@, @VENV_PYTHON@ substituted by install.sh)
│       ├── deskmate-brain.service
│       ├── deskmate-overlay.service
│       ├── deskmate-status.service
│       ├── deskmate-backup.service
│       └── deskmate-backup.timer   # Daily, Persistent=true
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
├── status_page/              # Standalone read-only status dashboard (separate process)
│   ├── app.py                # FastAPI service: overview, SSE log tails, event relay, mood history
│   └── static/                # index.html (markup), css/ (tokens, base, components, one per tab), js/ (ES modules; see docs/SPEC_STATUS_PAGE.md)
│
├── examples/
│   └── sample_look.json      # Reference LookSpec definition
│
├── config.example.toml       # Documented configuration template
├── pyproject.toml            # Python packaging and dependency declarations (source of truth)
├── requirements.txt          # Flat mirror of pyproject.toml's runtime dependencies and extras
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
voice = ["piper-tts>=1.8"]      # Installs the local TTS engine (and its `piper` entry point) into the venv
cloud = ["anthropic>=0.40"]     # Anthropic provider adapter; imported lazily, only needed if configured
```

`requirements.txt` mirrors the runtime dependencies plus the `voice` and `cloud` extras for plain `pip install -r` workflows; `pyproject.toml` remains the source of truth. The Python requirements are identical on every OS. Packages are installed into a project-local venv (never system pip) built by `deploy/sync_pip.sh`.

### 2. External System Binaries & Compatibility Baseline

The system requires the following external binaries discoverable on `PATH` or configured via `config.toml` (installed by `deploy/install.sh`):

| Dependency | Purpose | Target Version / Baseline |
|---|---|---|
| **Blender** | Headless 3D mesh compilation & asset inspection (`--inspect`). | Target: **5.0.1**; Compatibility Floor: **4.5 LTS+** |
| **Piper TTS** | Fast, local offline speech synthesis (`piper` CLI). Installed into the venv via the `voice` extra (`piper-tts`), not by apt; point `voice.tts.local.piper_bin` at the venv's absolute path for systemd. | $\ge 1.2.0$ (pip package $\ge 1.8$) |
| **Rhubarb Lip Sync** | Audio-to-phoneme waveform analysis invoked headlessly by `lipsync.py` (`rhubarb` CLI). No apt/pip package exists; download a release binary and set `voice.rhubarb_bin`. | $\ge 1.13.0$ |
| **ffmpeg** | Audio decoding support for the local STT stack. | Any recent release |
| **rsync** | Optional venv mirroring (`sync_pip.sh`) and daily state backup (`backup_state.sh`). | Any |
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

* Transparent, borderless Wayland surface managed via `LayerShellQt`. Sized to the configured `[presence] avatar_width_px` $\times$ `avatar_height_px` (default $240\times 240$), with an optional speech bubble beside it (`show_speech_bubble`).
* Pinned to `LayerShellQt::Window::LayerOverlay` with an exclusion zone of `-1` (QML property `exclusionZone`). Placement coordinates arrive in global virtual-desktop space and are converted to output-relative margins by the client (see `docs/SPEC_OVERLAY.md`).
* Operates in passive click-through mode (`KeyboardInteractivity = None`) using a screen-space input mask; switches to interactive mode (`KeyboardInteractivity = OnDemand`) when the interaction bar is summoned.
* Evaluates viseme weights directly against the local `QMediaPlayer` audio clock, eliminating network jitter from facial animation.
* Builds against Qt 6.8+ (`Core`, `Gui`, `Widgets`, `Qml`, `Quick`, `Quick3D`, `Quick3DAssetUtils`, `QuickControls2`, `WebSockets`, `Multimedia`, `Test`), `LayerShellQt`, and `KF6GlobalAccel`, with `cmake` 3.21+ and a C++20 compiler.
* Preloads and validates new assets in the background upon receiving `hot_reload_asset`, returning `asset_swap_result` to confirm or trigger rollbacks.

### 5. Desktop Sensing Adapter — V1 Reference (`adapters/kwin/`)

* Hooks KWin workspace signals (`windowActivated`, `frameGeometryChanged`, `screensChanged`).
* Debounces geometry dispatches by 100ms and explicitly ignores the overlay's own window (`resourceClass !== "deskmate-overlay"`).
* Dispatches active window bounding boxes and monitor geometries over session D-Bus (`org.deskmate.Presence`).

## Configuration Architecture (`config.toml`)

Operational settings are loaded from `$XDG_CONFIG_HOME/deskmate/config.toml` with live file watching (a failed reload keeps the last-good configuration). Every table rejects unknown keys, and secrets (`api_key = "env:VAR"`) are resolved only for entries that are actually enabled or in use:

* `[general]`: Logging verbosity (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`) and output routing.
* `[server]`: Host interface, port (`8765`), and `allowed_origins` for WebSocket security.
* `[storage]`: Scratch path (`workspace_dir`), persistent path (`deploy_dir`), and `backup_dir` (the daily backup target on a different disk, local or remote-mounted; read by `deploy/backup_state.sh`). The scratch and persistent paths both live on fast local storage and are **siblings** (neither nested inside the other, so a scratch cleanup can never wipe persistent state). `deploy_dir` holds `state.db` and must not live on a network filesystem (SQLite locking and WAL shared memory are unreliable over NFS); durability comes from the scheduled cold backup in `deploy/` instead. The Python venv builds and runs from `workspace_dir`.
* `[llm.local]`, `[llm.cloud.<provider>]`, `[llm.routing]`: Endpoints, models, timeouts, and capability tags.
* `[voice]`, `[voice.stt]`, `[voice.tts]`: Tool paths (`rhubarb_bin`, `piper_bin`), backend selections (`local` vs. `remote`), models, endpoints, and dynamic parameters. `[voice.tts.remote.<name>]` is a keyed table of remote TTS instances (per-instance `provider_type`, `priority`, `api_key`), tried highest priority first before local Piper.
* `[presence]`: Screen margins, display affinity, and avatar pixel dimensions.
* `[shortcuts]`: Global keybindings and `push_to_talk_mode = "hold" | "toggle"`.
* `[personality]`: Identity name and reflection/decay intervals (the trait and mood vocabularies are fixed constants, not configurable).
* `[skills]`: `enabled`, `disabled` (names), `max_actions_per_reply`: what the companion may *do* (structured replies, `brain/skills/`).
* `[status]`: host/port (`127.0.0.1:8780`), the `journalctl --user` units and extra log files the status page tails.
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
| **Displays vanish (screen lock / DPMS)** | The compositor destroys the overlay's layer-shell surfaces; the overlay stays alive (it does not quit when its last window closes) and re-shows and re-places the avatar when an output returns. | The companion reappears on unlock; the tray icon is the manual recovery. If the process dies for any other reason, the service restarts it. |
| **KWin / D-Bus absent** | `desktop_bridge.py` catches missing session bus. | Daemon defaults avatar placement to a safe margin corner on the primary display. | 
| **Cloud LLM drops** | `router.py` catches timeout / 5xx error. | Silently fails over to the local model tier; returns response with operational log. | 
| **Remote TTS / STT drops** | Voice manager catches connection failure. | Automatically redirects synthesis/transcription to local engines (`piper` / `faster-whisper`). | 
| **Blender build failure** | `blender_gen.py` encounters build error. | Retains current active `.glb` asset; logs failure without interrupting chat or presence loops. | 
| **Overlay swap failure** | Client reports `asset_swap_result: {ok: false}`. | Daemon rolls `manifest.json` back to previous version; companion informs user gracefully. | 