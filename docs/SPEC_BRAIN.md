# Spec: Brain Daemon (`brain/`)

## Component Role & Purpose

The Brain Daemon is the central orchestration and control plane for the Deskmate system. It is responsible for:

* Managing system configuration with zero-downtime hot reloading.
* Multi-tier, capability-aware language model routing (Local vs. Cloud providers).
* Persistent personality evolution, mood decay, and memory reflection.
* Voice pipeline orchestration: pluggable local/remote speech-to-text (STT), pluggable local/remote text-to-speech (TTS), and audio-to-viseme lip-sync generation.
* Headless procedural 3D asset generation orchestration via Blender, governing runtime redesigns per `docs/DESIGN_AVATAR_EVOLUTION_FLOW.md`.
* Window awareness, user presence tracking (`User_Active`, `User_Idle`, `User_Away`), and non-intrusive placement calculation via an abstracted desktop D-Bus bridge.
* Local WebSocket IPC server communication with frontend presentation clients.

The daemon exposes an asynchronous WebSocket IPC endpoint and an administrative HTTP surface, running continuously as a user-level background service.

---

## Scope: V1 and Beyond

### In Scope for V1

* Python daemon with structured configuration and continuous hot-reload watching.
* Multi-model routing across local (OpenAI-compatible) and cloud providers with capability matching and reasoning error guards.
* Persistent SQLite personality and agent state with long-term memory notes and reflection loops.
* Pluggable voice pipeline: local/remote STT, local/remote TTS backends, per-call voice overrides, and viseme timeline extraction.
* Abstracted desktop telemetry ingestion and presence state machine via session D-Bus (`org.deskmate.Presence`, `org.freedesktop.ScreenSaver`, and `logind`).
* WebSocket-based JSON event IPC for rendering overlay clients with handshake and Origin validation.
* Procedural Blender asset generation orchestration from declarative `LookSpec` schemas with atomic commit staging.
* User-level service lifecycle, health checks, and graceful degradation paths.

### Out of Scope for V1 (Documented Roadmaps)

* **Multi-Client & Remote Access:** Local loopback only (`127.0.0.1:8765`), secured against browser-origin hijacking. Private mesh networking (Tailscale) and mobile PWA access are defined in `docs/ADR_NETWORK_AND_MOBILE_STRATEGY.md`.
* **Autonomous Voice Selection & Cloning:** V1 implements baseline voice synthesis and interface accommodations; autonomous acoustic evolution is defined in `docs/ADR_AUTONOMOUS_VOICE_SELECTION.md`.
* **Skills & Sub-Agent Orchestration:** V1 reserves tool registration hooks, concurrency configuration keys (`max_parallel_slots`, `max_concurrency`, `max_script_workers`), and task state event schemas; deterministic script DAGs and cognitive sub-agents governed by the Inference Capacity Governor (ICG) are defined in `docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md`.
* **Dedicated Configuration GUI:** Direct editing of standard XDG TOML files (`config.toml`).

---

## Design Principles

### 1. No Machine-Specific Values in the Repository
All operational parameters, filesystem locations, server endpoints, and secrets are read from an external TOML configuration file:
* Default location: `$XDG_CONFIG_HOME/deskmate/config.toml` (typically `~/.config/deskmate/config.toml`).
* Runtime override: `DESKMATE_CONFIG_FILE` environment variable.
* Template with sensible defaults: `config.example.toml`.

### 2. Strongly Typed Schema & Validation
Configuration parsers, LLM client responses, look specifications, and IPC payloads are implemented with strongly typed data models (Pydantic models or Python `@dataclass` with type validation). Range validations (e.g. `Field(ge=0, le=100)`) are declared directly in Python schemas in `brain/config.py`.

### 3. Graceful Degradation
If a local model is unresponsive, a cloud provider quota is exceeded, remote voice endpoints fail, Blender generation fails, or desktop telemetry is absent, the daemon continues running safely in a degraded state without crashing.

### 4. Dynamic Configuration Reloading
The daemon runs a continuous filesystem watcher on `config.toml` (using `watchdog` or inotify). Operational parameter updates take effect immediately in memory without restarting the process.

---

## Configuration Schema (`brain/config.py`)

```toml
[general]
log_level = "INFO"
log_to_stdout = true

[server]
host = "127.0.0.1"                        # Interface binding for HTTP and WebSocket IPC
port = 8765                               # Port for daemon services (ws://<host>:<port>/ws/overlay)
allowed_origins = []                      # Whitelist for Origin header ([] = reject browser origins, accept native clients)

# Reserved network keys for Phase 2/3:
auth_token_file = ""                      # Path to token file (required if host != 127.0.0.1)
tls_cert = ""
tls_key = ""

[storage]
# Fast local scratch tier (ephemeral audio chunk caching, headless Blender build scratch):
workspace_dir = "<path_to_fast_scratch>"

# Long-term persistent deployment storage tier (SQLite state.db, manifests, active .glb assets):
deploy_dir = "<path_to_deploy_storage>"

[llm.local]
base_url = "http://127.0.0.1:11434/v1"   # OpenAI-compatible local endpoint (vLLM, Ollama, etc.)
model = "local-default-model"
timeout_s = 30.0
max_parallel_slots = 1                    # Total concurrent sequence slots (Slot 0 reserved for companion VIP)
extra_body = {}                           # Arbitrary engine flags passed into request bodies

[llm.cloud.<provider_name>]               # Keyed table for one or more cloud providers
provider_type = "generic_openai"          # "anthropic", "generic_openai", etc.
base_url = "https://api.example.com/v1"
model = "cloud-reasoning-model"
api_key = "env:DESKMATE_CLOUD_API_KEY"    # Direct string or environment reference
timeout_s = 60.0
max_concurrency = 4                       # Max parallel calls allowed to this provider
capabilities = ["code", "complex_reasoning", "large_context"]
priority = 10                             # Higher = prioritized for matching capabilities
enabled = true

[llm.routing]
escalation_threshold = "auto"             # Routing classifier mode ("auto", "always_local", "always_cloud")
classification_timeout_s = 3.0            # Timeout budget for local intent classifier

[llm.routing.classifier]
base_url = ""                             # e.g., "http://127.0.0.1:11434/v1" (defaults to llm.local.base_url)
model = ""                                # e.g., "small-fast-classifier" (defaults to llm.local.model)
timeout_s = 5.0                           # (defaults to classification_timeout_s or llm.local.timeout_s)
extra_body = {}                           # (defaults to llm.local.extra_body)

# Reserved skills configuration keys for Phase 2/3:
# [skills]
# max_script_workers = 4                   # Process pool limit for Track 1 script tasks

[voice.stt]
active_backend = "local"                  # "local" or "remote"
silence_threshold_ms = 800                # Trailing silence required to trigger transcription (toggle mode only)

[voice.stt.local]
engine = "faster-whisper"                 # Local offline STT engine identifier
model_size = "base.en"                    # Model footprint (tiny, base, small)
device = "auto"                           # "cpu", "cuda", "auto"
compute_type = "int8"

[voice.stt.remote]
base_url = "http://127.0.0.1:8000"        # Standalone HTTP STT server or OpenAI-compatible endpoint
endpoint = "/v1/audio/transcriptions"
model = "whisper-1"                       # Remote model identifier
timeout_s = 10.0
api_key = ""                              # Optional direct string or "env:VAR_NAME"
params = {}                               # Arbitrary engine parameters (language, prompt, temperature)

[voice.tts]
active_backend = "local"                  # "local" or "remote"
voices_dir = "<deploy_dir>/assets/voices" # Base path for offline voice assets
volume = 1.0
rate = 1.0

[voice.tts.local]
engine = "piper"                          # Local offline TTS binary/engine
voice_model_path = "voices/default.onnx"
voice_config_path = "voices/default.onnx.json"

[voice.tts.remote]
base_url = "http://127.0.0.1:8000"        # Standalone HTTP TTS server (e.g. tts-serve)
endpoint = "/synthesize"
voice_id = "default_voice"
timeout_s = 10.0
capabilities_endpoint = "/capabilities"   # Optional discovery endpoint for supported fields/voices
params = {}                               # Arbitrary engine flags (speed, emotion, temperature)

# Reserved voice catalog stubs for V2+ autonomous voice selection:
# [voice.voice_catalog.<id>]
# backend = "local_piper"
# model_path = "voices/en_US-lessac-medium.onnx"
# display_name = "Lessac"
# traits = { formality = 75, extraversion = 50 }

[presence]
dbus_service_name = "org.deskmate.Presence"
dbus_object_path = "/org/deskmate/Presence"
avatar_width_px = 320
avatar_height_px = 320
margin_px = 24
display_affinity = "secondary"            # Preferred display: "primary", "secondary", "auto"
idle_after_s = 600                        # Inactivity seconds to trigger User_Idle state

[shortcuts]
toggle_interaction = "Meta+Alt+D"         # Opens / dismisses the floating input bar
push_to_talk       = "Meta+Alt+M"         # Microphone capture shortcut
push_to_talk_mode  = "hold"               # "hold" or "toggle"

[personality]
identity_name = "Deskmate"
trait_names = [
  "openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism",
  "honesty_humility", "humor", "formality", "boldness", "affection", "patience", "sarcasm", "loyalty"
]
mood_names = ["energy", "valence", "stress"]
reflection_interval_s = 3600              # Frequency of background memory passes
mood_decay_interval_s = 60                # Frequency of mood relaxation passes
mood_half_life_s = 1800                   # Half-life for mood axes to decay toward baseline

[appearance]
blender_bin = "blender"                   # Path to Blender executable (Target: 5.0.1, Floor: 4.5 LTS+)
default_look_spec = "examples/sample_look.json"  # Initial bootstrap LookSpec template
keep_recent_versions = 3                  # Number of recent .glb build revisions to retain in storage
redesign_cooldown_hours_min = 12          # Minimum cooldown between autonomous redesigns
redesign_cooldown_hours_max = 24          # Maximum cooldown between autonomous redesigns
autonomous_redesign_probability = 0.5     # Chance of triggering redesign on eligible reflection cycle
```

---

## LLM Pool & Capability Routing (`brain/llm_pool.py`, `brain/router.py`)

### 1. Client Abstraction (`brain/llm_pool.py`)
All LLM integrations implement the abstract interface:

```python
class LLMClient(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], **kwargs) -> str:
        """Sends conversation history to the model and returns the response text."""
        pass
```

* **`LocalLLMClient`:** Connects to any local server exposing the standard OpenAI-compatible `/v1/chat/completions` API. Merges `extra_body` options into payloads. Explicitly validates that `choices[0].message.content` is non-empty; if reasoning tokens exhaust the budget and output is null, it raises a descriptive runtime error.
* **Cloud Adapters:** Hosted in an adapter registry (`anthropic`, `generic_openai`, etc.), translating payloads to provider schemas. Cloud SDKs are imported lazily so local-only setups require no cloud dependencies.

### 2. Failover Pool & Cooldown Policy (`LLMPool`)
`LLMPool` wraps configured providers sorted by priority:
* **Transient Timeout:** On a network timeout (`timeout_s`), the pool attempts **1 immediate retry** against the same provider. If that retry *also* times out, the pool marks the provider failed, engages the 60-second cooldown timer, and fails over immediately to the next provider.
* **Rate Limit or Auth Failure:** On HTTP 429 (Rate Limit) or HTTP 401/403 (Auth/Token invalid), the pool does not retry. It immediately flags the provider with a 60-second cooldown timer (`cooldown_until = now() + 60.0`) and routes directly to the next eligible provider in priority order.
* **Exhaustion Fallback:** If all cloud options are in cooldown or failed, execution routes cleanly to `LocalLLMClient`.
* **Standard Operational Log:** On failover, the pool emits:
  ```
  WARNING: LLM provider '<provider_name>' failed (<reason>); failing over to '<next_provider>'
  ```

### 3. Capability-Aware Router & Tool Registry (`brain/router.py`)
1. **Classification Stage:** Evaluates complexity using `[llm.routing.classifier]` (defaulting to `[llm.local]`). Bypassed if `escalation_threshold` is `"always_local"` or `"always_cloud"`.
   * *Classifier Fallback Rule:* If the classifier times out or returns an error, the router defaults to local execution (`escalated = false`) with an operational warning log. Flaky classification never crashes chat.
2. **Provider Selection:** Queries matching capability tags (`code`, `complex_reasoning`, `large_context`) when escalation is warranted.
3. **Extensible Tool Registry (V1 Accommodation for V2 Skills):**
   * Exposes an open registration method `register_tool(name: str, description: str, parameters: dict, handler: Callable)` in `router.py`, allowing V2 skills to plug in dynamically alongside V1's hardcoded `change_appearance` and `revert_appearance`.
   * Built-in V1 tools: `change_appearance(request: str)` and `revert_appearance()`.
   * Models lacking native tool calling are supported via a lightweight intent classifier and keyword pre-filter.
4. **Return Envelope:** Yields `{text, provider, escalated, rationale}`.

---

## Personality & Agent State Engine (`brain/personality/`)

State is stored in SQLite (`state.db`) within `deploy_dir`. Six tables partition the data with explicit DDL:

```sql
CREATE TABLE IF NOT EXISTS traits (
    name TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 50.0
);

CREATE TABLE IF NOT EXISTS mood (
    name TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 50.0,
    baseline REAL NOT NULL DEFAULT 50.0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS identity (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    provider TEXT,
    escalated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

### Canonical `agent_state` Keys
While the `agent_state` table uses a generic key-value schema (which requires zero DDL migrations when adding new state fields), the following keys are authoritatively reserved and managed by the system:
* `current_voice_id`: Active voice identifier (`piper:<stem>` or `remote:<id>`).
* `previous_voice_id`: Voice identifier prior to last mutation (for zero-latency revert).
* `staged_voice_id`: Voice selected during reflection while `User_Away`, committed at reveal.
* `voice_pinned`: Boolean flag (`"true"` / `"false"`) suppressing autonomous voice evolution.
* `staged_version`: Integer version of a `.glb` compiled while `User_Away`, awaiting reveal.
* `last_redesign_timestamp`: Epoch timestamp of the last visual/acoustic reveal commit.
* `base_template_since`: Epoch timestamp of when the active `base_template` was adopted.

### Memory, Reflection & Prompt Synthesis
* **Reflection Loop (`reflect.py`):** Runs every `reflection_interval_s`. Analyzes new interactions, proposes $\le 5$ memory notes, and computes bounded deltas:
  * Maximum trait delta: $\pm 5$ per cycle.
  * Maximum mood delta: $\pm 20$ per cycle.
* **Memory Retrieval Strategy (V1):** When synthesizing prompts, `prompts.py` retrieves the **last 20 memory notes by recency**, boosted by keyword overlap matching against tokens in the active user turn. (Semantic vector retrieval is deferred to V2).
* **Prompt Synthesis (`prompts.py`):**
  * Traits/mood in neutral range ($30\text{–}70$) are omitted to prevent system prompt bloat.
  * Extremes ($\le 29$ or $\ge 71$) map to explicit stylistic directives.
  * Injects active look summary: `[Appearance: <base>, wearing <outfit>, <accessories>, palette <colors>]`.

---

## Voice & Speech Pipeline (`brain/voice/`)

* **STT (`stt.py`):** Coordinates incoming audio. Push-to-talk release (`audio_stream_end`) finalizes transcription immediately; `silence_threshold_ms` acts as a trailing endpointing fallback in toggle mode. Failover redirects remote STT errors to local `faster-whisper`.
* **Microphone Wire Format:** Ingestion expects 16 kHz mono PCM16LE audio chunks transmitted as raw binary WebSocket messages, paired with JSON `audio_stream_chunk` sequence headers.
* **TTS (`tts.py`):** Dispatches synthesis requests to `TTSBackend.synthesize(text, out_path, voice_id=..., params=...)`. Voice ID precedence: `agent_state.current_voice_id` $\to$ `config.toml`. Failover redirects remote HTTP errors to local Piper `default.onnx`.
* **Viseme Extraction (`lipsync.py`):** Invokes the headless `rhubarb` CLI tool (`-f json -r phonetic <wav_path>`) to extract time-stamped phonetic letter sequences (`A`–`H`, `X`) from generated `.wav` audio. Outputs are mapped to Deskmate's canonical 3D mesh morph targets (`viseme_A`–`viseme_X`) via `blender/viseme_shapekeys.py` (pure Python module). Extraction is bypassed completely if `has_face == false`.
* **Latency & Chunking:** Generates `speech` envelopes per sentence boundary for streaming dialogue, delivering `{utterance_id, chunk_seq, final, wav_path, audio_url, delivery, visemes}`.
* **Prosody & Delivery Mapping:** Deterministically maps traits and mood to delivery gestures (`deadpan` if sarcasm $\ge 70$, `animated` if energy $\ge 70$, `subdued` if energy $\le 30$, `explanatory` for code/lengthy explanations).

---

## Appearance & Blender Orchestration (`brain/appearance/`)

Governed by `docs/DESIGN_AVATAR_EVOLUTION_FLOW.md`:

1. **LookSpec Validation (`spec.py`):** Asserts fields against catalog definitions (`templates_catalog.json`, `wardrobe_catalog.json`). Retries synthesis once on failure before graceful abort.
2. **Headless Execution (`blender_gen.py`):** Centralizes background subprocess execution via `asyncio.create_subprocess_exec` with environment sanitization (stripping `VIRTUAL_ENV`, `PYTHONPATH`, `PYTHONHOME`) and a **120-second timeout**:
   ```bash
   blender --background --python blender/generator.py -- --spec <spec> --out <out>
   ```
3. **Subprocess Sandboxing Standard (Accommodation for V2 Skills):** Background script execution utilities standardize on Bubblewrap (`bwrap`) isolation on Linux. If `bwrap` is missing on a host attempting to run sandboxed subprocesses, the execution utility fails fast with `RuntimeError("Bubblewrap ('bwrap') is required for sandboxed skills. Install 'bubblewrap' via your system package manager.")`.
4. **Option B Staging & Atomic Reveal:**
   * If compiled while `User_Away`, asset is staged in `<deploy_dir>/assets/staged/look-v{N}.glb` with `staged_version` recorded in `agent_state`. Live `manifest.json` is untouched.
   * Upon transition to `User_Active`, asset is moved to `<deploy_dir>/assets/look-v{N}.glb`, hardlinked to `data/current/`, committed atomically via `manifest.json`, and revealed conversationally.
5. **AssetManifest Schema (`manifest.json`):**
   ```json
   {
     "current_version": 3,
     "versions": [
       {
         "version": 3,
         "glb_filename": "look-v3.glb",
         "sha256": "4a7b...",
         "created_at": 1718000000.0,
         "status": "ok",
         "has_face": true,
         "speech_indicator_mode": "viseme_shapekeys",
         "capabilities": {"has_head": true, "has_arms": true, "has_legs": true, "has_face": true},
         "viseme_shape_keys": ["viseme_A", "viseme_B", "viseme_C", "viseme_D", "viseme_E", "viseme_F", "viseme_G", "viseme_H", "viseme_X"],
         "animation_clips": [{"name": "idle_lookaround", "loop": true, "duration_s": 4.0}, {"name": "move", "loop": true, "duration_s": 1.0}, {"name": "think", "loop": true, "duration_s": 3.0}],
         "bounds": {"height_m": 1.65, "width_m": 0.55, "depth_m": 0.32},
         "look_spec": { /* Full LookSpec JSON for zero-recompile rollback */ }
       }
     ]
   }
   ```

---

## Presence & Window Placement (`brain/presence/`)

### 1. Telemetry Bridge & User Presence (`desktop_bridge.py`)
* Implements `org.deskmate.Presence` (`UpdateState(json)`).
* Subscribes to system/session D-Bus signals for user activity state:
  * `org.freedesktop.ScreenSaver` / `org.kde.screensaver` (`ActiveChanged`).
  * `org.freedesktop.login1.Session` (`LockedHint`, `IdleHint`).
* State Machine:
  * **`User_Away`:** Screensaver active, session locked, or display sleeping.
  * **`User_Idle`:** No window/pointer activity for $> \text{idle\_after\_s}$ (default 10m).
  * **`User_Active`:** Active window interaction and unlocked session.

### 2. Non-Intrusive Placement (`placement.py`)
Returns screen-local `Placement(x, y, screen_name, hidden)`:
* **No Active Window:** Bottom-right margin corner of primary monitor.
* **Active Window:** Evaluates display corners in sequence (`bottom-right`, `bottom-left`, `top-right`, `top-left`); selects first with zero collision.
* **Maximized / Colliding:** Migrates to secondary display matching `display_affinity`.
* **Fullscreen:** Migrates to secondary screen; if single display, emits `hidden = true`.

---

## IPC Server & Security (`brain/ipc/server.py`)

### 1. Connection Handshake & Security
* **Origin & Host Protection:** Verifies HTTP `Origin` header (must match `allowed_origins`; native clients omitting `Origin` are accepted). Validates `Host` header against `127.0.0.1:8765` and `localhost:8765` to block DNS-rebinding.
* **Client Handshake:**
  * Client sends `hello`:
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
  * Daemon replies with `welcome`:
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
              "sha256": "4a7b...",
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
* **Client ID Collision:** Drops older socket with close code `4409` (`Client ID Replaced`).

### 2. Standardized Event Payload Schemas

```json
// Daemon -> Client
{"type": "chat_response", "timestamp": 1718000000.0, "payload": {"utterance_id": "utt_1", "text": "Hello!", "provider": "local", "escalated": false, "rationale": "chat"}}
{"type": "set_position", "timestamp": 1718000000.0, "payload": {"x": 100, "y": 200, "screen_name": "DP-1", "duration_ms": 400, "hidden": false}}
{"type": "status_update", "timestamp": 1718000000.0, "payload": {"state": "idle" | "thinking" | "rebuilding" | "listening" | "speaking"}}
{"type": "mood_update", "timestamp": 1718000000.0, "payload": {"energy": 60.0, "valence": 50.0, "stress": 20.0}}
{"type": "play_gesture", "timestamp": 1718000000.0, "payload": {"clip": "greet", "loop": false}}
{"type": "config_update", "timestamp": 1718000000.0, "payload": {"presence": {}, "shortcuts": {}}}

// Reserved V2+ Sub-Agent Task & Permission Events (Daemon -> Client):
{"type": "agent_task_progress", "timestamp": 1718000000.0, "payload": {"task_id": "t_1", "plan_id": "p_1", "description": "Scanning repository", "percent": 45}}
{"type": "agent_task_complete", "timestamp": 1718000000.0, "payload": {"task_id": "t_1", "plan_id": "p_1", "status": "completed", "result_summary": "Done"}}
{"type": "agent_permission_request", "timestamp": 1718000000.0, "payload": {"task_id": "t_1", "skill_id": "terminal", "tool_name": "bash", "command_preview": "git clean -fd", "risk_level": "critical"}}

// Client -> Daemon
{"type": "user_text_input", "timestamp": 1718000000.0, "payload": {"text": "Hello", "client_id": "desktop_primary"}}
{"type": "audio_stream_start", "timestamp": 1718000000.0, "payload": {"stream_id": "s_1", "sample_rate": 16000, "encoding": "pcm16le", "channels": 1}}
{"type": "audio_stream_chunk", "timestamp": 1718000000.0, "payload": {"stream_id": "s_1", "seq": 0}}
{"type": "audio_stream_end", "timestamp": 1718000000.0, "payload": {"stream_id": "s_1"}}
{"type": "interaction_state", "timestamp": 1718000000.0, "payload": {"state": "active" | "idle", "client_id": "desktop_primary"}}

// Reserved V2+ Sub-Agent Permission Response (Client -> Daemon):
{"type": "agent_permission_response", "timestamp": 1718000000.0, "payload": {"task_id": "t_1", "decision": "allow_once" | "always_allow" | "deny"}}
```

### 3. Administrative REST Surface (`brain/main.py`)

The daemon exposes an asynchronous FastAPI HTTP service. FastAPI automatically synthesizes and serves interactive OpenAPI 3.1 specifications (`/openapi.json` and `/docs`) directly from Pydantic models.

The primary administrative endpoints and concrete wire payloads include:

#### `GET /health`
* **Status:** `200 OK`
* **Response Body:**
  ```json
  {
    "status": "ok",
    "name": "Deskmate",
    "active_voice": "piper:en_US-lessac",
    "voice_degraded": false,
    "presence": "User_Active",
    "traits": {
      "openness": 50.0,
      "formality": 75.0,
      "extraversion": 50.0
    },
    "mood": {
      "energy": 60.0,
      "valence": 50.0,
      "stress": 20.0
    }
  }
  ```

#### `POST /chat`
* **Headers:** `Content-Type: application/json`
* **Request Body:**
  ```json
  {
    "text": "What is the capital of Japan?",
    "speak": true
  }
  ```
* **Status:** `200 OK`
* **Response Body:**
  ```json
  {
    "status": "ok",
    "utterance_id": "utt_1718000000_1"
  }
  ```
* **Behavior:** Processes dialogue through the router, logs to SQLite `interactions`, broadcasts `chat_response`, and emits `speech` (with synthesized audio and visemes) over WebSocket.

#### `POST /appearance/generate`
* **Headers:** `Content-Type: application/json`
* **Request Body:** LookSpec JSON payload.
* **Status:** `202 Accepted`
* **Response Body:**
  ```json
  {
    "status": "building",
    "task": "appearance_generation"
  }
  ```

#### `POST /appearance/revert`
* **Status:** `200 OK`
* **Response Body:**
  ```json
  {
    "status": "ok",
    "reverted_to_version": 2
  }
  ```

#### `POST /reflect`
* **Status:** `200 OK`
* **Response Body:**
  ```json
  {
    "status": "ok",
    "notes_created": 2,
    "trait_deltas": {"formality": 2.0}
  }
  ```

#### Streaming & Asset Endpoints:
* `GET /audio/{utterance_id}.wav` $\to$ Streams temporary utterance audio (`audio/wav`).
* `GET /assets/{filename}` $\to$ Streams runtime `.glb` models from `data/current/` (`model/gltf-binary`).
* `WS /ws/overlay` $\to$ Presentation WebSocket hub.

#### Reserved V2 Administrative Endpoints:
* `GET /voice/catalog` $\to$ Returns the active in-memory voice catalog parsed from `config.toml`.
* `POST /voice/pin` $\to$ Body `{"pinned": bool}`; freezes/unfreezes autonomous voice evolution in `agent_state`.
* `POST /voice/revert` $\to$ Restores previous active voice from `agent_state.previous_voice_id`.
* `DELETE /skills/{skill_id}/permissions` $\to$ Clears persistent SQLite pre-approved tool execution permissions.
* `POST /tasks/{task_id}/cancel` $\to$ Cancels an active sub-agent task or plan DAG and terminates child processes.

---

## Subsystem Verification & Testing (`brain/tests/`)

### Hermetic CI Test Harness Patterns
Automated unit and integration test runs must execute with zero external network dependencies, no local GPU/audio hardware, and complete isolation:
* **HTTP & Cloud Mocking:** Use `httpx.MockTransport` or custom transport handlers to intercept HTTP requests to local/cloud LLM endpoints, remote TTS (`tts-serve`), and remote STT servers.
* **Mock Subclass Adapters:** Use mock subclasses of `LLMClient`, `TTSBackend`, and `STTBackend` to test routing, timeout failovers, and cooldown logic deterministically in memory.
* **In-Memory SQLite:** Use SQLite in-memory databases (`sqlite3.connect(":memory:")`) to test database initialization, trait clamping, exponential decay math, and state persistence with microsecond execution speed.

### Test Suites
* **`test_config.py`:** Validates TOML parsing, hot reloading, and fallback on syntax error. Asserts reserved concurrency keys (`max_parallel_slots`, `max_concurrency`, `max_script_workers`) are parsed.
* **`test_router.py`:** Asserts local routing, capability escalation, cloud failover (1 timeout retry, 60s cooldown on 429), empty-completion guards, and open tool registration via `register_tool`.
* **`test_personality.py`:** Tests SQLite bootstrapping, DDL table creation, identity persistence, exponential decay math, and reflection clamping ($\pm 5$ traits, $\pm 20$ mood).
* **`test_voice.py`:** Asserts per-call overrides on `synthesize()`, STT/TTS failovers, and viseme timing calculations.
* **`test_appearance.py`:** Asserts LookSpec validation, 120s timeout handling, Option B staged holding, atomic manifest commits, and Bubblewrap missing-binary detection.
* **`test_presence.py`:** Asserts corner placement logic, multi-monitor migration, and D-Bus logind/screensaver telemetry parsing.
* **`test_ipc.py`:** Tests Origin/Host validation, client handshake (`hello` $\to$ `welcome`), duplicate `client_id` closure (`4409`), dual-delivery payload generation, and unhandled-event graceful passthrough for reserved agent task/permission envelopes (`plan_id` verification).