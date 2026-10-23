# Deskmate

Deskmate is an interactive, on-screen desktop companion agent. It lives on screen, avoids obstructing your active windows, routes conversation between local and cloud language models, speaks with real-time lip-synced speech through a procedurally generated 3D avatar, and evolves a persistent personality over time.

The system is designed to be reusable, modular, and **OS/desktop-environment agnostic**: the core daemon talks to the outside world only through open contracts (a local WebSocket protocol for presentation clients, an abstracted telemetry bridge for desktop sensing), so any platform capable of implementing those contracts can host the companion. Every machine-specific path, credential, and setting is abstracted into a user-editable configuration file rather than hard-coded. Concrete platform implementations — which operating systems, window managers, and rendering engines are actually wired up — ship incrementally; see [Versions](#versions) below for what's targeted where.

## Table of Contents

* [What it does](#what-it-does)
* [Architecture](#architecture)
* [Requirements](#requirements)
  * [All platforms (Python)](#all-platforms-python)
* [Repository layout (target)](#repository-layout-target)
* [Documentation](#documentation)
* [License](#license)
* [Versions](#versions)
  * [V1](#v1)
  * [V2+](#v2)

## What it does

* **Sits on your desktop without getting in the way.** A transparent, click-through 3D overlay tracks window and monitor geometry and repositions itself — or migrates to a secondary display — to avoid obstructing whatever you're working in.
* **Talks to you.** Push-to-talk or typed input is transcribed (local or remote speech-to-text), routed to a local or cloud LLM depending on task complexity, and spoken back with synthesized speech (local or remote text-to-speech).
* **Lip-syncs and gestures in real time.** Speech audio is analyzed into a viseme timeline and played back against the local audio clock, driving facial morph targets and body gestures with no network jitter.
* **Has a procedurally generated body.** A headless Blender pipeline compiles a declarative `LookSpec` into a fully rigged, animated `.glb` 3D character — base idles, mood idles, talking gestures, and movement/thinking loops included.
* **Does things, not just talks.** Replies are structured: only the dialogue is spoken, and a separate list of actions (gestures, remembering a fact, changing its look) runs through an allowlisted skill registry that validates every call.
* **Shows its work.** A standalone status page (`http://127.0.0.1:8780/`) shows service health, the live decision pipeline (routing, model reasoning, skills), a mood/trait timeline, chat and logs.
* **Develops a personality.** A reflection loop maintains traits, mood, and long-term memories in local SQLite state, decaying moods and proposing small, gated changes over time.
* **Is configuration-driven.** All runtime behavior — LLM endpoints and routing, voice backends, presence margins, shortcuts, personality traits — lives in a single hot-reloaded `config.toml`.
* **Is platform-agnostic by contract.** The Brain daemon talks to presentation clients over a local WebSocket protocol and to desktop environments over an abstracted telemetry bridge. Any renderer that can consume the protocol (glTF 2.0 assets, named morph targets, audio, WebSockets) or any desktop environment that can emit window/monitor geometry can serve as a frontend or sensing backend — without changes to the core daemon.

## Architecture

| Component | Role | Path |
|---|---|---|
| **Brain Daemon** (Python) | Core orchestration: config watching, LLM routing, personality & memory, voice pipeline, Blender orchestration, presence/placement, WebSocket IPC hub. Platform-independent. | `brain/` |
| **Parametric Asset Generator** (Blender, headless) | Compiles `LookSpec` definitions into rigged, animated, viseme-capable `.glb` avatars. Platform-independent. | `blender/` |
| **Desktop Overlay** | Rendering client: avatar display, audio-driven lip-sync, text/mic input, hot-reloading assets. Swappable — any engine implementing the IPC protocol can act as the frontend. | `overlay/` |
| **Desktop Sensing Adapter** | Reports window/monitor geometry over a telemetry bridge, debounced and filtered to exclude the overlay itself. One adapter per platform, swappable per desktop environment/OS. | `adapters/` |
| **Deployment** | Service supervision and install/sync scripts. | `deploy/` |

```
┌─────────────────────────────┐        Local WebSocket IPC (JSON envelopes) ┌─────────────────────────────┐
│  overlay/                   │◄──────────────────────────────────────────► │  brain/  (Python daemon)    │
│  Frontend rendering client  │   Position cmds, speech + visemes, inputs   │  Router, personality,       │
│  (swappable per platform)   │───────────────────────────────────────────► │  voice, appearance, presence│
└─────────────▲───────────────┘                                             └─────┬───────────┬───────────┘
              │ Window geometry updates                             OpenAI-compat │           │  Cloud LLM
              │ (excluding overlay)                                  /v1 endpoint │           │  APIs
┌─────────────┴───────────────┐                                                   ▼           ▼
│ Desktop sensing adapter      │                                             Local LLM      Cloud LLM(s)
│ (swappable per platform)     │  Telemetry bridge (org.deskmate.Presence)  (Fast tier)   (Complex tier)
│ Debounced active windows,   │─────────► brain/presence/desktop_bridge.py
│ screens, and desktop state  │
└─────────────────────────────┘

blender/generator.py  ◄── Invoked headless by brain/appearance/blender_gen.py ── Consumes LookSpec
   │                      (Triggered at setup or on autonomous redesign)
   └─ Exports .glb (Mesh + Armature + Visemes + State Clips)
         │
         ▼
data/current/  ──────── Hot-reloaded by overlay via IPC notification ────────►  Runtime asset store
```

Full component specs live in `docs/SPEC_BRAIN.md`, `docs/SPEC_BLENDER.md`, and `docs/SPEC_OVERLAY.md`; the avatar redesign flow is detailed in `docs/DESIGN_AVATAR_EVOLUTION_FLOW.md`.

## Requirements

The Brain daemon and asset pipeline are platform-independent in design, but
every concrete implementation that currently exists in this repo (the
reference overlay client, the KWin sensing adapter, the deploy scripts)
targets Linux. The breakdown below is **per-OS**: what you need to install
to build and deploy this repo on each platform, not an abstract dependency
list. Package names are for Debian/Ubuntu (`apt`); other distros will have
differently-named equivalents of the same packages.

Local inference (e.g. an OpenAI-compatible endpoint via Ollama/vLLM/llama.cpp)
and/or cloud LLM API credentials are required for conversation; the daemon
supports either or both, with routing between them configured in
`config.toml`.

### All platforms (Python)

The Brain daemon's Python requirements are the same on every OS:

* Python 3.11+
* Python packages, declared in `pyproject.toml` and mirrored in `requirements.txt`. Install them into a project-local venv (not system pip) — on Linux via `deploy/sync_pip.sh`, elsewhere with `pip install -r requirements.txt` inside your own venv:
  * core: `fastapi`, `uvicorn[standard]`, `websockets`, `pydantic`, `watchdog`, `httpx`, `jeepney`, `faster-whisper`, `soundfile`, `numpy`
  * `voice` extra: `piper-tts` (installs the local TTS engine itself — no separate binary/apt package)
  * `cloud` extra: `anthropic` (only needed for an `anthropic` LLM provider)
  * `dev` extra: `pytest`, `pytest-asyncio`, `ruff`, `mypy`
  * Everything runtime-related in one go: `deploy/sync_pip.sh -r requirements.txt`; or via extras: `deploy/sync_pip.sh -e '.[dev,voice,cloud]'`

### Linux (reference platform)

**To run the Brain daemon** (`brain/`, `blender/`), in addition to the
[Python requirements above](#all-platforms-python) — only implemented/tested
on Linux:

* [Blender](https://www.blender.org/) 4.5 LTS+ (5.0.1 is the development target) — for headless `.glb` avatar generation
* `ffmpeg` — audio decoding for `faster-whisper` (local STT)
* [Rhubarb Lip Sync](https://github.com/DanielSWolf/rhubarb-lip-sync/releases) 1.13.0+ — no apt/pip package exists; download a release binary and point `voice.rhubarb_bin` at it in `config.toml`
* `bubblewrap` (`bwrap`) — sandboxed script/skill execution (reserved for V2+, not yet used by any V1 code path)

  ```
  sudo apt install ffmpeg bubblewrap
  ```

**To additionally build the reference overlay client** (`overlay/`, C++20/Qt):

* `cmake` 3.21+ and a C++20 compiler (`build-essential`)
* Qt 6.8+ LTS, specifically the `Core`, `Gui`, `Widgets`, `Qml`, `Quick`, `Quick3D`, `Quick3DAssetUtils`, `QuickControls2`, `WebSockets`, `Multimedia`, and `Test` modules
* `LayerShellQt` (the layer-shell Wayland window-positioning library)
* `KF6GlobalAccel` — global-shortcut registration (`org.kde.kglobalaccel`)
* `qml6-module-qt-labs-platform` — runtime QML module for the system-tray icon (`Qt.labs.platform`); `Widgets` is needed because its menu is widget-based

  ```
  sudo apt install cmake build-essential \
      qt6-base-dev qt6-declarative-dev qt6-quick3d-dev qt6-quick3d-dev-tools \
      qt6-quicktimeline-dev qt6-websockets-dev qt6-multimedia-dev \
      liblayershellqtinterface-dev libkf6globalaccel-dev \
      qml6-module-qt-labs-platform
  ```

**To additionally run the reference desktop integration** (KDE Plasma):

* KDE Plasma 6 on Wayland — `kpackagetool6` (installs the KWin script adapter, `adapters/kwin/`) and `kwriteconfig6`/`busctl` (enabling it) ship with Plasma itself, nothing extra to install

**To deploy** (`deploy/install.sh`, `deploy/sync_pip.sh`, systemd units):

* `systemd` (user units) — standard on most Linux distros
* `rsync` — used by `deploy/sync_pip.sh`'s optional venv mirroring and `deploy/backup_state.sh`'s state backups

`deploy/install.sh` checks for the runtime binaries above (Blender, Piper,
Rhubarb, Bubblewrap) itself and prints the exact fix if one's missing.

### macOS

None — no code in this repo targets macOS yet. Nothing to install. See
[V2+](#v2) for the roadmap (an engine-agnostic sensing adapter and overlay
client could in principle be built for it, but none exists today).

### Windows

None — no code in this repo targets Windows yet. Nothing to install. See
[V2+](#v2) for the roadmap.

## Repository layout (target)

```
deskmate/
├── brain/          # Python core daemon (routing, personality, voice, appearance, presence, IPC)
├── blender/        # Headless parametric 3D asset generator
├── overlay/        # Rendering client (reference frontend implementation)
├── status_page/    # Standalone read-only status dashboard
├── adapters/       # Desktop sensing adapters, one per platform
│   └── kwin/       # V1 reference adapter (KDE Plasma 6 / KWin)
├── deploy/         # Service supervision and install/sync scripts
├── docs/           # Architecture specs and decision records
├── examples/       # Reference LookSpec and other sample definitions
├── config.example.toml
├── pyproject.toml
└── requirements.txt
```

See [`docs/SPEC_OVERVIEW.md`](docs/SPEC_OVERVIEW.md) for the full target layout, dependency manifest, configuration schema, and system verification strategy.

## Documentation

* [`docs/SPEC_OVERVIEW.md`](docs/SPEC_OVERVIEW.md) — project summary, architecture, repository layout, dependencies, and verification strategy.
* [`docs/SPEC_BRAIN.md`](docs/SPEC_BRAIN.md) — Brain daemon: routing, personality, voice, presence, IPC.
* [`docs/SPEC_BLENDER.md`](docs/SPEC_BLENDER.md) — parametric asset generator: template resolution, wardrobe, animation, budgets.
* [`docs/SPEC_STATUS_PAGE.md`](docs/SPEC_STATUS_PAGE.md) — the standalone status dashboard.
* [`docs/SPEC_OVERLAY.md`](docs/SPEC_OVERLAY.md) — overlay client and desktop sensing adapter.
* [`docs/DESIGN_AVATAR_EVOLUTION_FLOW.md`](docs/DESIGN_AVATAR_EVOLUTION_FLOW.md) — how and when the avatar's appearance changes.
* [`docs/ADR_NETWORK_AND_MOBILE_STRATEGY.md`](docs/ADR_NETWORK_AND_MOBILE_STRATEGY.md) — multi-client network and mobile roadmap.
* [`docs/ADR_AUTONOMOUS_VOICE_SELECTION.md`](docs/ADR_AUTONOMOUS_VOICE_SELECTION.md) — autonomous voice evolution and cloning roadmap.
* [`docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md`](docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md) — skills and sub-agent orchestration roadmap.

## License

See [`LICENSE`](LICENSE).

## Versions

The core daemon, IPC protocol, and asset contracts are platform-agnostic by design. Each version targets concrete reference implementations on top of that agnostic core; later versions add platforms and capabilities without breaking earlier contracts.

### V1

*Current target: Linux desktop companion.*

* **Desktop integration (reference platform):** Linux, KDE Plasma 6 on Wayland, via a KWin script adapter (`adapters/kwin/`) and session D-Bus bridge (`org.deskmate.Presence`). Contracts are engine-agnostic so other environments can be added later as sibling adapters, without changing the core daemon.
* **Presentation client (reference implementation):** C++20 / Qt 6.8+ LTS (`QtQuick3D`, `LayerShellQt`), connected over local WebSocket IPC. Any client implementing the same protocol can substitute it.
* **Brain Daemon (`brain/`):** configuration hot-reload, dual-tier local/cloud LLM routing, personality evolution & reflection, pluggable STT/TTS, Blender asset orchestration, window-presence/placement algorithms, WebSocket IPC hub.
* **Parametric Asset Generator (`blender/`):** headless procedural `LookSpec` → rigged, viseme-capable, animated `.glb` pipeline (targets Blender 5.0.1; 4.5 LTS+ compatibility floor).
* **Voice:** user-configured default voice model with per-call voice overrides supported by the daemon.
* **Networking:** binds strictly to local loopback (`127.0.0.1:8765`), secured against browser-origin hijacking; protocol is "network-ready" (dual local-path/HTTP URL fields) for future phases.
* **Configuration:** direct editing of `$XDG_CONFIG_HOME/deskmate/config.toml` with live, zero-downtime hot reload. No dedicated graphical settings UI.
* **Deployment:** user-level systemd units and install/sync scripts.

### V2+

*Roadmap: multi-platform, multi-surface, extensible.*

* **Additional desktop environments & operating systems** — the engine-agnostic contracts extend beyond the V1 KDE/Wayland reference to other Linux desktop environments (GNOME, Hyprland) and other operating systems (macOS, Windows), each via its own sensing adapter plugged into the same telemetry bridge.
* **Alternate presentation clients** — other rendering engines (e.g. Godot, Tauri/WebGPU) can implement the same IPC protocol in place of the V1 Qt reference client.
* **Network & mobile multi-client access** ([`docs/ADR_NETWORK_AND_MOBILE_STRATEGY.md`](docs/ADR_NETWORK_AND_MOBILE_STRATEGY.md)) — private mesh exposure (Tailscale/WireGuard) with per-device bearer tokens, then a zero-install mobile PWA client (Three.js/WebGL) for interacting with the same companion from other devices, with the Brain daemon remaining the single source of truth.
* **Autonomous voice selection & cloning** ([`docs/ADR_AUTONOMOUS_VOICE_SELECTION.md`](docs/ADR_AUTONOMOUS_VOICE_SELECTION.md)) — the companion evolves its voice alongside its visual appearance during autonomous redesigns, choosing between preset voice personas or cloned voices, subject to user-configured safeguards.
* **Skills & sub-agent orchestration** ([`docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md`](docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md)) — a 3-track model for extending the companion beyond conversation: deterministic sandboxed script pipelines, single-turn tool calls, and autonomous multi-turn sub-agents (e.g. "audit this repo and fix the tests") coordinated through an Inference Capacity Governor so background work never starves the interactive voice/chat loop.
* **Dedicated settings UI** — a graphical alternative to direct `config.toml` editing.
