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

Configuration models share a strict base (`extra="forbid"`): an unknown or misspelled TOML key is a loud validation error at load time, never a silently dropped field. Fields with a fixed set of valid values are typed as enums/`Literal`s (e.g. `log_level` is a `LogLevel` string enum; `voice.stt.local.engine` and `voice.tts.local.engine` are `Literal`s naming the one engine V1 implements) so a typo fails at startup rather than surfacing later as an obscure runtime error. `env:VAR` secret resolution (`api_key` fields) is gated on whether the owning entry is actually in use — `enabled = true` for `[llm.cloud.<name>]` and `[voice.tts.remote.<name>]`, `active_backend = "remote"` for `[voice.stt.remote]` — and is performed in a model-level validator on the model that owns the gating field, so a documentation placeholder such as `api_key = "env:SOME_UNSET_VAR"` on a disabled or inactive entry never blocks daemon startup.

### 3. Graceful Degradation
If a local model is unresponsive, a cloud provider quota is exceeded, remote voice endpoints fail, Blender generation fails, or desktop telemetry is absent, the daemon continues running safely in a degraded state without crashing.

### 4. Dynamic Configuration Reloading
The daemon runs a continuous filesystem watcher on `config.toml` (using `watchdog` or inotify). Operational parameter updates take effect immediately in memory without restarting the process. On each change the file is re-parsed and re-validated into a new settings object that is swapped in atomically; if parsing or validation fails, the failure is logged and the **last-good configuration keeps serving**. `get_settings()` returns the live instance and `subscribe(callback)` lets `main.py` rebuild pools on reload.

---

## Configuration Schema (`brain/config.py`)

```toml
[general]
log_level = "INFO"                        # DEBUG | INFO | WARNING | ERROR | CRITICAL
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
# Fast local scratch tier (ephemeral audio chunk caching, headless Blender build scratch, and the Python venv).
# Must be a sibling of deploy_dir, never nested inside it, so scratch cleanup cannot touch persistent state:
workspace_dir = "<path_to_fast_scratch>"

# Long-term persistent deployment storage tier (SQLite state.db, manifests, active .glb assets).
# Keep on fast local storage, not a network filesystem (SQLite locking/WAL are unreliable over NFS);
# durability comes from the scheduled cold backup (deploy/backup_state.sh, deskmate-backup.timer):
deploy_dir = "<path_to_deploy_storage>"

# Where the daily cold backup goes: a path on a different disk (another drive in this machine, or a remote
# location reached through a network mount), so it survives losing deploy_dir's drive. Read by deploy/backup_state.sh,
# not by the daemon. $DESKMATE_BACKUP_DIR overrides it; blank falls back to a same-machine folder with a warning.
# The target's parent must already exist (an unmounted drive or share must not turn into a backup onto the local
# disk), the script warns when the target is on the same physical disk as deploy_dir, and the target should be
# outside the git repo or gitignored.
backup_dir = "<path_to_backup_target>"

[llm.local]
base_url = "http://127.0.0.1:11434/v1"   # OpenAI-compatible local endpoint (vLLM, Ollama, etc.)
model = "local-default-model"
timeout_s = 30.0
max_parallel_slots = 1                    # Total concurrent sequence slots (Slot 0 reserved for companion VIP)
extra_body = {}                           # Arbitrary engine flags passed into request bodies
reasoning = false                         # Let the model think before answering; its reasoning is streamed to the status page, never spoken
reasoning_extra_tokens = 2048             # Added to each call's max_tokens while reasoning is on (thinking spends from that budget)

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

[skills]                                  # What the companion may do, not just say (brain/skills/)
enabled = true                            # false = replies are plain dialogue, no actions
disabled = []                             # Skill names to switch off individually
max_actions_per_reply = 3
# Reserved for Phase 2/3:
# max_script_workers = 4                   # Process pool limit for Track 1 script tasks

[status]                                  # The standalone status page (status_page/); read by that service only
host = "127.0.0.1"                        # Keep local: it shows chat history and logs
port = 8780
units = ["deskmate-brain", "deskmate-overlay"]   # journalctl --user units shown as log panes
extra_logs = {}                           # name -> file path (e.g. tts-serve server.log) shown as extra log panes

[voice]
rhubarb_bin = "rhubarb"                   # Path to the Rhubarb Lip Sync binary (no system package exists; usually a manual release download)
lipsync_recognizer = "phonetic"           # "phonetic" (fast, ~1s/sentence) or "pocketSphinx" (English-tuned but ~real time: delays speech)

[voice.stt]
active_backend = "local"                  # "local" or "remote"
silence_threshold_ms = 800                # Trailing silence required to trigger transcription (toggle mode only)

[voice.stt.local]
engine = "faster-whisper"                 # Local offline STT engine (V1 implements only faster-whisper; widen the Literal when adding another)
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
active_backend = "local"                  # "local" = never contact remote TTS; "remote" = try enabled remote instances first, local as fallback
voices_dir = "<deploy_dir>/assets/voices" # Base path for offline voice assets
volume = 1.0                              # Companion voice level, 0.0-1.0 (linear amplitude on the overlay's audio output; validated). Applied live — see config_update
rate = 1.0

[voice.tts.local]
engine = "piper"                          # Local offline TTS engine (V1 implements only piper; widen the Literal when adding another)
piper_bin = "piper"                       # Absolute path recommended: a systemd unit running the venv's python directly does not have the venv's bin/ on PATH
voice_model_path = "default.onnx"         # Relative to voices_dir
voice_config_path = "default.onnx.json"   # Relative to voices_dir

[voice.tts.remote.<name>]                 # Keyed table: one or more remote TTS instances, tried by priority
provider_type = "tts_serve"               # Wire contract this instance speaks: "tts_serve" or "generic_openai" (see TTS notes below)
base_url = "http://127.0.0.1:7500"        # One running TTS instance
endpoint = "/synthesize"
capabilities_endpoint = "/capabilities"   # Discovery endpoint; "" = none (TOML has no null, so blank means absent)
priority = 10                             # Higher = tried first, same convention as [llm.cloud.<provider_name>]
enabled = true
timeout_s = 10.0
api_key = ""                              # Optional direct string or "env:VAR_NAME"; resolved only when enabled = true
params = {}                               # Arbitrary engine flags (speed, emotion, temperature)

[voice.tts.remote.<name>.seed_voices]    # Optional (tts_serve only): seed pools by body type, see "Seed-Based Voice Selection"
male = []                                 # e.g. [7, 88, 200]
female = []
male_robot = []
female_robot = []
# default_seed = 5                       # Optional: used instead of a random pick while the look has default femininity; omit for none

# Reserved voice catalog stubs for V2+ autonomous voice selection:
# [voice.voice_catalog.<id>]
# backend = "local_piper"
# model_path = "voices/en_US-lessac-medium.onnx"
# display_name = "Lessac"
# traits = { formality = 75, extraversion = 50 }

[presence]
dbus_service_name = "org.deskmate.Presence"
dbus_object_path = "/org/deskmate/Presence"
avatar_width_px = 240                     # Companion size in px (96-1024). Drives BOTH daemon corner placement and the overlay's window/avatar/bar size (sent in `welcome`)
avatar_height_px = 240
show_speech_bubble = true                 # Show the sentence being spoken in a bubble beside the avatar
margin_px = 24
display_affinity = "secondary"            # Preferred display: "primary", "secondary", "auto"
idle_after_s = 600                        # Inactivity seconds to trigger User_Idle state

[shortcuts]
toggle_interaction = "Meta+Alt+D"         # Opens / dismisses the floating input bar
push_to_talk       = "Meta+Alt+M"         # Microphone capture shortcut
push_to_talk_mode  = "hold"               # "hold" or "toggle"

[personality]
identity_name = "Deskmate"                # Initial name, seeded only into a brand-new state.db (see Personality section)
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

**Fixed (non-configurable) vocabularies.** The 13 personality trait names (`openness`, `conscientiousness`, `extraversion`, `agreeableness`, `neuroticism`, `honesty_humility`, `humor`, `formality`, `boldness`, `affection`, `patience`, `sarcasm`, `loyalty`) and the 3 mood axes (`energy`, `valence`, `stress`) are module-level constants in `brain/config.py` (`TRAIT_NAMES`, `MOOD_NAMES`), **not** `[personality]` keys. `prompts.py` pairs each exact name with hand-written prompt phrasing and `state.py` seeds one SQLite row per name, so an editable list would only ever safely support reordering or dropping names — a false flexibility. Because config models forbid unknown keys, a `trait_names`/`mood_names` entry in `config.toml` is a validation error. `identity_name` is the one genuinely user-customizable value in `[personality]`.

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

* **`LocalLLMClient`:** Connects to any local server exposing the standard OpenAI-compatible `/v1/chat/completions` API. Merges `extra_body` options into payloads. When `reasoning = true` it forces `enable_thinking` on (overriding `extra_body`) and adds `reasoning_extra_tokens` to `max_tokens`. Whatever the server returns as reasoning (`reasoning_content` or `reasoning`) is emitted as an `llm.reasoning` event and never becomes dialogue; each call also emits `llm.call` (latency, token counts). The classifier client is built without `reasoning`. Explicitly validates that `choices[0].message.content` is non-empty; if reasoning tokens exhaust the budget and output is null, it raises a descriptive runtime error.
* **Cloud Adapters:** Hosted in an adapter registry (`anthropic`, `generic_openai`, etc.), translating payloads to provider schemas. Cloud SDKs are imported lazily so local-only setups require no cloud dependencies (the `anthropic` SDK ships as the optional `cloud` packaging extra).

### 2. Failover Pool & Cooldown Policy (`LLMPool`)
`LLMPool` wraps configured providers, each registered as a `(name, client, capabilities)` entry, sorted by priority. The local tier is a singleton pool (`[llm.local]` is a single required table), so the router treats both tiers uniformly as pools of size 1 and N.
* **Transient Timeout:** On a network timeout (`timeout_s`), the pool attempts **1 immediate retry** against the same provider. If that retry *also* times out, the pool marks the provider failed, engages the 60-second cooldown timer, and fails over immediately to the next provider. Timeout is the only failure class that is retried.
* **Rate Limit or Auth Failure:** On HTTP 429 (Rate Limit) or HTTP 401/403 (Auth/Token invalid), the pool does not retry. It immediately flags the provider with a 60-second cooldown timer (`cooldown_until = now() + 60.0`) and routes directly to the next eligible provider in priority order.
* **Any Other Failure:** Connection errors, HTTP 5xx, the empty-completion error, and any other unanticipated exception take the same no-retry cooldown-and-fail-over path as 429/401/403, so the failover loop never needs to enumerate error types.
* **Exhaustion Fallback:** If all cloud options are in cooldown or failed, execution routes cleanly to `LocalLLMClient`. A pool whose every candidate is already cooling down raises immediately without calling any of them (hammering providers mid-cooldown would defeat the cooldown); the router treats that as exhaustion.
* **Capability Preference:** `chat()` accepts a keyword-only `required_capability`, consumed by the pool (never forwarded into the provider request payload). Providers whose `capabilities` include it are tried first, in priority order; all remaining enabled providers follow, also in priority order. This is a preference, not a hard filter — if nothing is tagged for the need, escalation still proceeds to the best available provider.
* **Standard Operational Log:** On failover, the pool emits:
  ```
  WARNING: LLM provider '<provider_name>' failed (<reason>); failing over to '<next_provider>'
  ```

### 3. Capability-Aware Router & Tool Registry (`brain/router.py`)
1. **Classification Stage:** Evaluates complexity using `[llm.routing.classifier]` (defaulting to `[llm.local]`; a dedicated small, fast model on a separate device is recommended so classification does not compete with the chat model). Bypassed if `escalation_threshold` is `"always_local"` or `"always_cloud"`.
   * *Classifier Output Contract:* The classifier is asked for a single JSON object `{"escalate": bool, "capability": "code" | "complex_reasoning" | "large_context" | "general", "reason": "<under 8 words>"}` with a token budget of 128. The router extracts the first `{...}` substring before parsing, tolerating models that wrap or follow their answer with commentary. An unrecognized `capability` value normalizes to `"general"`.
   * *Classifier Fallback Rule:* If the classifier times out, returns an error, returns unparseable JSON (including a malformed value inside the braces), or omits the `escalate` key, the router defaults to local execution (`escalated = false`) with an operational warning log. This is deliberately the opposite of a fail-safe-toward-cloud policy: flaky classification never crashes chat and never silently spends cloud quota.
2. **Provider Selection:** When escalation is warranted, the classifier's `capability` tag is passed to the cloud pool as `required_capability` (see Capability Preference above); `"general"` applies no preference and uses plain priority order.
3. **Extensible Tool Registry (V1 Accommodation for V2 Skills):**
   * Exposes an open registration method `register_tool(name: str, description: str, parameters: dict, handler: Callable)` in `router.py`, allowing V2 skills to plug in dynamically alongside V1's hardcoded `change_appearance` and `revert_appearance`.
   * Built-in V1 tools: `change_appearance(request: str)` and `revert_appearance()`. `build_router()` installs placeholder handlers; `brain/evolution.py`'s `AvatarEvolution.wire_tools(router)` re-registers both names with the real handlers (`register_tool` is a plain name-keyed assignment, so re-registration overwrites).
   * Models lacking native tool calling are supported via a lightweight intent classifier and keyword pre-filter.
4. **Return Envelope:** Yields `{text, provider, escalated, rationale, capability}`; `capability` is the classifier's tag and is meaningful only when `escalated = true`.

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
    escalated INTEGER NOT NULL DEFAULT 0,
    reflected INTEGER NOT NULL DEFAULT 0      -- 0 = pending reflection, 1 = already reflected on
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

-- Time series behind the status page's mood/trait timeline.
CREATE TABLE IF NOT EXISTS personality_history (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,           -- 'mood' | 'traits'
    name TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_personality_history_ts ON personality_history(kind, ts);
```

`personality_history` is written by `PersonalityStore._record()` on every deliberate change (`nudge_mood`, `adjust_traits`, each taking an optional `cause`, e.g. `"reflection"`) and a startup snapshot. Routine `decay_mood` ticks are recorded (and announced as `personality.mood` events) only when a value has drifted at least 0.25 or five minutes have passed since the last point, so the table stays small; rows older than 30 days are pruned at startup. It is observation: a failure here is swallowed and never blocks a mood update.

`interactions.reflected` is the per-row cursor that lets the reflection loop examine only interactions it has not yet processed; no `agent_state` key substitutes for it. `escalated` is never null (user turns store `0`). `memories` carries no weight column: relevance weighting happens at prompt-assembly time, not in storage.

On first boot only, `state.py` seeds one `traits` row per `TRAIT_NAMES` entry, one `mood` row per `MOOD_NAMES` entry, and the `identity` name from `[personality] identity_name`, all via `INSERT OR IGNORE`. A later autonomous self-rename (`set_identity_name()`) therefore persists across restarts instead of being reset to whatever `config.toml` says.

### Canonical `agent_state` Keys
While the `agent_state` table uses a generic key-value schema (which requires zero DDL migrations when adding new state fields), the following keys are authoritatively reserved and managed by the system:
* `current_voice_id`: Active voice identifier (`piper:<stem>` or `remote:<id>`). Written by seed-based voice selection (see the Voice & Speech Pipeline section) as `remote:<instance>:seed:<n>` when a remote instance defines seed pools; `GET /health`'s `active_voice` reports it. When no seed pools apply the key stays unset and `active_voice` is derived from the live TTS configuration. The wider autonomous voice evolution (staging, pinning, trait scoring) remains V2+ (`docs/ADR_AUTONOMOUS_VOICE_SELECTION.md`).
* `previous_voice_id`: Voice identifier prior to last mutation (for zero-latency revert).
* `staged_voice_id`: Voice selected during reflection while `User_Away`, committed at reveal.
* `voice_pinned`: Boolean flag (`"true"` / `"false"`) suppressing autonomous voice evolution.
* `staged_version`: Integer version of a `.glb` compiled while `User_Away`, awaiting reveal.
* `last_redesign_timestamp`: Epoch timestamp of the last visual/acoustic reveal commit. Stamped only when a change is actually revealed (never at staging time), so a cooldown cannot complete during a long `User_Away` period while built-but-unrevealed assets pile up. Unset means "never redesigned": the cooldown counts as elapsed, so a fresh install is eligible from its first reflection cycle.
* `base_template_since`: Epoch timestamp of when the active `base_template` was adopted. Unset is treated as "the 7-day hold is not proven", never as "long enough".

### Memory, Reflection & Prompt Synthesis
* **Reflection Loop (`reflect.py`):** Runs every `reflection_interval_s`. Analyzes new interactions, proposes $\le 5$ memory notes, and computes bounded deltas:
  * Maximum trait delta: $\pm 5$ per cycle.
  * Maximum mood delta: $\pm 20$ per cycle.
  * Out-of-range LLM output is clamped, never rejected. `run_reflection()` returns a `ReflectionResult` (`changed`, `trait_deltas`, `mood_deltas`, `notes_created`; truthy when `changed`), which `POST /reflect` serializes.
  * **Autonomous redesign gate:** after a reflection pass, `maybe_trigger_autonomous_redesign()` draws the cooldown fresh on every eligibility check (`random.uniform(redesign_cooldown_hours_min, redesign_cooldown_hours_max)` hours versus time since `last_redesign_timestamp`) rather than persisting a sampled threshold — statistically equivalent to sampling once per cycle, and it needs no extra persisted state.
* **Memory Retrieval Strategy (V1):** When synthesizing prompts, `prompts.py` retrieves the **last 20 memory notes by recency**, boosted by keyword overlap matching against tokens in the active user turn. The candidate set is exactly those 20 most recent notes; `rank_memory_notes()` stably re-sorts them by keyword-overlap count against the active user turn (ties, including zero overlap, keep recency order). Overlap therefore only reorders within the window and never pulls in an older note. (Semantic vector retrieval is deferred to V2).
* **Prompt Synthesis (`prompts.py`):**
  * Traits/mood in neutral range ($30\text{–}70$) are omitted to prevent system prompt bloat.
  * Extremes ($\le 29$ or $\ge 71$) map to explicit stylistic directives.
  * Injects active look summary: `[Appearance: <base>, wearing <outfit>, <accessories>, palette <colors>]`. The summary is produced by `look_summary()` in `brain/appearance/spec.py` (`prompts.py` deliberately knows nothing about `LookSpec` internals) and passed in by `main.py`, which assembles the system prompt per turn.

---

## Voice & Speech Pipeline (`brain/voice/`)

* **STT (`stt.py`):** Coordinates incoming audio. Push-to-talk release (`audio_stream_end`) finalizes transcription immediately; `silence_threshold_ms` acts as a trailing endpointing fallback in toggle mode. Failover redirects remote STT errors to local `faster-whisper`. `build_stt_coordinator(settings)` constructs the coordinator from a loaded `Settings` (mirroring `build_router`); with `active_backend = "local"` the network is never touched.
  * *Local engine:* `faster-whisper` loads its model lazily on the first `transcribe()` call, so wiring up the daemon never requires network access (model resolution contacts the Hugging Face Hub). Audio is decoded in-process — WAV read with the standard library, resampled to 16 kHz with numpy — and handed to the model as an array, bypassing faster-whisper's own file-decoding path (PyAV), whose version coupling is fragile.
  * *Endpointing:* The energy-based voice-activity check uses a fixed RMS threshold (`_VOICE_ACTIVITY_RMS_THRESHOLD = 500.0`, computed by hand over PCM16 samples since the stdlib `audioop` module no longer exists in Python 3.13+). It is a constant, not a config key. `StreamingSTT.is_trailing_silence_exceeded()` is a polled check with no timer of its own; `main.py`'s background loop polls it in toggle mode and calls `end_stream()`, since a toggle-mode client never signals silence explicitly.
* **Microphone Wire Format:** Ingestion expects 16 kHz mono PCM16LE audio chunks transmitted as raw binary WebSocket messages, paired with JSON `audio_stream_chunk` sequence headers.
* **TTS (`tts.py`):** Dispatches synthesis requests to `TTSBackend.synthesize(text, out_path, voice_id=..., params=...)`. Voice ID precedence: `agent_state.current_voice_id` $\to$ `config.toml`. `[voice.tts.remote.<name>]` is a keyed table (mirroring `[llm.cloud.<provider_name>]`'s pattern, including the **higher-priority-first** ordering) so multiple remote TTS instances can be configured and tried in priority order before falling back to local Piper `default.onnx` — e.g. several engine instances brought online opportunistically across different hosts as GPU capacity allows.
  * *Pool construction:* `build_tts_pool(settings)` honors `[voice.tts] active_backend`: `"local"` skips remote instances entirely (never touching the network, matching STT's policy), while `"remote"` builds the priority-ordered pool of enabled instances with local Piper as the final fallback. `TTSPool.is_degraded` is true when remotes are configured but all are currently cooling down, forcing the local fallback; `GET /health` surfaces it as `voice_degraded`.
  * *Provider types:* each instance names its wire contract via `provider_type`, resolved through a registry in `brain/voice/tts_remote.py` that mirrors `brain/llm_pool.py`'s `build_cloud_client()`. `"tts_serve"` (default; [tts-serve](https://github.com/scorbo2/tts-serve), a standalone process bundling REST API and model): `POST <endpoint>` with `{text, audio_base64?, reference_text?, language?, seed?, **params}`, returning `{audio_base64, sample_rate, seed, time_used, rtf}` JSON (not raw WAV bytes) — the client base64-decodes the response before writing `out_wav_path`; `capabilities_endpoint` corresponds to tts-serve's own `GET /capabilities` discovery contract. `"generic_openai"`: the OpenAI `/v1/audio/speech` contract — `POST <endpoint>` with `{model, input, voice, response_format, **params}` returning raw audio bytes with no JSON wrapper and no discovery endpoint (`capabilities_endpoint = ""`); it typically needs `api_key` (sent as a bearer token), resolved from `env:VAR` only while the instance is `enabled`.
  * *Voice precedence (per request, engine-supported parts only):* 1. `voice_sample` (the entry's reference WAV, sent to `tts_serve` as `audio_base64` plus optional `voice_sample_text` as `reference_text`; rejected for `generic_openai`), 2. a seed, only from that entry's `seed_voices` (`tts_serve` only), 3. the engine's default voice (a preset such as Pocket TTS's `voice` is an ordinary engine param in `params`). An entry with a `voice_sample` is skipped by seed selection, and a sample on a `tts_serve` request removes any `seed`.
  * *Per-instance params:* `TTSPool.synthesize(..., remote_params={instance: {...}})` sends params only to the named instance, merged over its configured `params` and any shared `params`.
  * *Local engine:* `piper_bin` and the voice model paths are resolved like `rhubarb_bin`/`blender_bin` (explicit binary path; model paths joined onto `voices_dir`).
* **Seed-Based Voice Selection (`voice_select.py`):** Engines such as VoxCPM have no fixed voice unless given a reference clip: each request without one invents a voice from a random seed, so successive sentences can sound like different people (a child, a woman, a man). The user therefore curates **seed pools** per body type under `[voice.tts.remote.<name>.seed_voices]` (`male`, `female`, `male_robot`, `female_robot`; seeds are meaningful only for the engine/model of that entry, and `tts_serve` restricts them to its valid range, 1–1000). `SeedVoiceSelector` is evaluated at the start of each utterance, never mid-sentence:
  1. Pick the highest-priority enabled `tts_serve` instance that defines any pool, and only when `[voice.tts] active_backend = "remote"`.
  2. Map the active `LookSpec` to pool groups, most specific first: `standard_humanoid_male` $\to$ `male`; `standard_humanoid_female` $\to$ `female`; any other base takes its gender from `proportions.femininity` ($\ge 0.5$ reads female); `primitive_mechanical` tries `<gender>_robot` first and falls back to the plain gendered pool when no robot seeds are configured. The first non-empty group is the candidate set.
  3. If `agent_state.current_voice_id` already names a seed from that instance that is in the candidate set, keep it — the voice is stable across sentences, utterances, and restarts. Otherwise, if `seed_voices.default_seed` is set, is in the candidate set, and the look still has the default `proportions.femininity` (0.5, i.e. uncustomized), use it so a fresh install always starts on a known voice; otherwise choose one candidate at random (V1; trait-based scoring is the V2+ replacement), write the previous value to `previous_voice_id`, and persist the new `current_voice_id`. A redesign that changes the body type, or an edit to the pools, therefore re-picks automatically.
  The chosen seed is passed as `remote_params` for that instance only, merged over its configured `params`, so it never leaks to a failover instance running a different engine. A selection failure is logged and speech proceeds with the engine's default voice. Local Piper fallback has a single fixed voice, so a fallback sentence may differ in gender from the selected seed voice.
* **Live Voice Volume (`[voice.tts] volume`):** A linear 0.0–1.0 amplitude multiplier for the companion's voice, applied by the overlay on its audio output (so it stacks with the system mixer's volume for the overlay's stream, and with `deploy/wireplumber/50-deskmate-overlay.conf`'s separate saved mixer volume). It travels in `welcome.config.voice.volume`; and because the daemon's config watcher reloads `config.toml` on save, `main.py` subscribes to reloads and, when the overlay-relevant subset (`welcome_config_subset`, same shape as `welcome.config`) changed, pushes it as `config_update` to connected overlays and refreshes the hub's settings so later `welcome`s are current — no restart needed. Unrelated edits send nothing. Values outside 0.0–1.0 are rejected at validation (the last-good config keeps serving).
* **Viseme Extraction (`lipsync.py`):** Invokes the headless `rhubarb` CLI tool (`rhubarb -r <recognizer> -f json <wav_path>`, where `<recognizer>` is `[voice] lipsync_recognizer`) to extract time-stamped phonetic letter sequences (`A`–`H`, `X`) from generated `.wav` audio. Outputs are mapped to Deskmate's canonical 3D mesh morph targets (`viseme_A`–`viseme_X`) via `blender/viseme_shapekeys.py` (pure Python module). Extraction is bypassed completely if `has_face == false`.
* **Latency & Chunking:** A reply is never synthesized as one request. `_speak()` splits it with `split_sentences()` (boundary regex `[^.!?]*[.!?]+`; a trailing fragment without a terminator is its own sentence; fragments with no word characters are dropped), then synthesizes, lip-syncs, and broadcasts one `speech` envelope per sentence in order, each as soon as it is ready: `{utterance_id, chunk_seq, final, wav_path, audio_url, text, delivery, visemes}`, with `audio_url = /audio/{utterance_id}_{chunk_seq}.wav` and `final` true only on the last sentence. Time-to-first-audio is therefore one sentence's synthesis, the client plays sentence N while the daemon synthesizes N+1, and no single request has to fit a whole reply inside a remote engine's `timeout_s` (remote engines run near real time, so a multi-sentence reply synthesized at once would time out and fall back to Piper). A sentence whose synthesis fails is skipped and logged; the remaining sentences are still spoken.
  * *Recognizer speed matters:* extraction runs once per sentence and the sentence cannot play before it finishes, so its duration is added to every sentence's latency. Rhubarb's English-tuned `pocketSphinx` recognizer measured ~6.4 s for a 5.8 s clip (about real time), roughly doubling each sentence's delay and leaving a gap before and between sentences; the language-independent `phonetic` recognizer took ~1.3 s on the same clip with a similar spread of mouth shapes, which suffices for a small stylized mouth, so `phonetic` is the default and `pocketSphinx` an opt-in for accuracy.
  * *Stereo output (`stereo.py`):* every backend returns mono, and the overlay's Qt audio stack places a mono source on its own (on a 5.1 sink that can be front-center). So `ensure_stereo_wav()` rewrites each chunk's WAV as two identical channels after lip-sync (rhubarb reads the mono original) and just before the `speech` envelope is broadcast; already-stereo files are untouched, and a conversion failure is logged and the chunk is sent as-is.
  * *Pipelining:* synthesis is strictly sequential (one engine), but each sentence's lip-sync runs as a background task while the *next* sentence synthesizes, and envelopes are broadcast in chunk order as each lip-sync completes (a later sentence whose lip-sync finishes first still waits for its predecessor). Doing lip-sync inline instead would add its full duration to every sentence.
  * *Observability:* every sentence logs `speech <utterance> chunk i/n: backend=<name|local> seed=<n> synth=<s>s chars=<n>` at INFO, so which engine and seed produced each sentence is visible in the daemon log (journal unit `deskmate-brain`). A request that falls back to Piper additionally logs a WARNING (`voice fallback: ... spoken by local Piper`); a failing remote logs a WARNING with the exception type (an HTTP timeout stringifies to empty, so the type is always included) and the cooldown, and requests skipped because every remote is cooling down log a WARNING with the remaining time. Without these the voice would change silently.
  * Trade-off: a short sentence followed by a long one can leave an audible pause while the long one synthesizes.
* **Prosody & Delivery Mapping:** Deterministically maps traits and mood to delivery gestures (`deadpan` if sarcasm $\ge 70$, `animated` if energy $\ge 70$, `subdued` if energy $\le 30$, `explanatory` for code/lengthy explanations). When several rules match, content shape wins first (`explanatory` for long or code-bearing text), then the trait/mood rules in the order listed above; if nothing matches the delivery is `animated`. Implemented as `pick_delivery()` in `brain/voice/delivery.py`, which depends only on personality data, not on any TTS backend.
* **Plain-Speech Filter (`plaintext.py`):** the reply is spoken and shown in a bubble, so `to_plain_speech()` strips Markdown (bold, headings, lists, code fences, links, quotes, rules), emoji and ASCII emoticons, turning list/paragraph breaks into sentence breaks. The system prompt asks for plain talk; the filter is the backstop. Applied once in `_handle_chat_text`, so the spoken text, bubble, stored chat and `chat_response` agree.
  * **Vocal cues:** a span in single asterisks is a vocal cue, emphasis or a silent action. Neither TTS engine accepts emotion or non-verbal tags (Pocket has no style controls; VoxCPM only a voice-design prefix, ignored when cloning), so a cue is rewritten as a sound the engine can say: `*sigh*` $\to$ "Ahhh...", `*laughs*` $\to$ "Ha ha ha.", `*clears throat*` $\to$ "Ahem." (table `_VOCAL_CUES`, tuned by ear). A single non-cue word keeps its word (`a *huge* fan`); a longer span with no cue is a silent action and is dropped. Emotion beyond that comes only from word choice and punctuation.
* **Failure Domains in Speech Delivery:** TTS synthesis and viseme extraction fail independently. A TTS failure aborts the utterance (no audio exists to announce). A lipsync failure (e.g. `rhubarb` missing or erroring on a clip) degrades to an empty `visemes` list — a static mouth — while still broadcasting the real `audio_url`, `wav_path`, and `delivery`.

---

## Structured Replies & Skills (`brain/skills/`)

The companion can *do* things, not only talk, and the model's output keeps the two apart. This is the V1 realisation of the "Track 2" single-turn tool call from `docs/ADR_SKILLS_AND_AGENT_ORCHESTRATION.md`; the ADR's governor, sandboxing, permission flow and sub-agents remain deferred.

* **Reply contract (`reply.py`):** when skills are enabled the system prompt (`SkillRegistry.prompt_block()`) tells the model to answer with only `{"say": "<spoken text>", "do": [{"skill": "<name>", "args": {...}}]}`. `say` is dialogue and is the only thing spoken or shown in the bubble; `do` is optional.
  * `parse_reply()` is forgiving in exactly one direction. It tolerates a code fence and text around the object, and extracts the first JSON object. If that is missing, malformed, or has no string `say`, the **entire reply is treated as dialogue and no action runs**; model text is never guessed into an action (a JSON object that merely appears in dialogue, e.g. a code sample, therefore does nothing). Malformed `do` entries are skipped individually.
  * Replies that came from the router's keyword tools (`source` begins with `tool:`) bypass parsing: the tool's string result is the dialogue.
* **Registry (`registry.py`):** a skill is `Skill(name, description, handler, params)`; `Param` is text, optionally restricted to an `enum`. The model proposes, the registry disposes: a call runs only if the skill is registered and not in `[skills] disabled`, and its arguments pass validation (no unknown keys, required keys present, text only, enum membership, at most 300 chars). Unknown skills, invalid arguments and calls beyond `max_actions_per_reply` are refused and reported as `skill.rejected` events; they are never executed. Handlers are async, receive the validated args and the app state, run in order under an 8 s timeout, and a failing handler never stops the others or the reply. Results are shown on the status page and never spoken.
* **Built-in skills (`builtin.py`):** `gesture(name: greet|agree|disagree|celebrate|shrug)` broadcasts `play_gesture` (`name` is what the overlay reads, `clip` the documented field); `remember(note)` stores a memory note; `change_look(request)` and `revert_look()` call the `AvatarEvolution` handlers. None can reach a shell or the filesystem.
* **Flow (`main.py:_handle_chat_text`):** build prompt (with the skills block) $\to$ router $\to$ `parse_reply` $\to$ run actions $\to$ plain-speech filter $\to$ log/broadcast/speak `say`. An actions-only reply is not spoken and not logged as a chat turn.
* **Fire-and-forget (V1 limit):** a skill's result is not fed back to the model. A skill that must answer a question needs a second model call; not built. Drop-in skill files (the ADR's `skill.toml`) are likewise not built: loading code from a directory would run whatever is in it, so trust and sandboxing come first.
* **Config:** `[skills] enabled` (default `true`; `false` = plain dialogue only), `disabled = [names]`, `max_actions_per_reply` (default 3, 1-10).
* **Adding a skill:** register one `Skill` (see `builtin.py`). The `description` goes into the model's prompt verbatim.

---

## Event Stream (`brain/events.py`)

An in-process event bus that narrates the daemon's work so the standalone status page (`status_page/`) can show it live. It is observation only: nothing in the daemon reads events back.

* **Hard rules (it sits on the hot path):** `emit()` never blocks, awaits or raises (any failure is swallowed); with no subscriber it is a bounded `deque` append (2000-event backlog). Each subscriber has a bounded queue (500) and loses its *oldest* events when full, so a stuck consumer cannot slow the daemon.
* **Turn correlation:** `begin_turn()` (called at the start of `_handle_chat_text`) stores a turn id in a contextvar, so `emit()` calls deep in the router or an LLM client are tagged with the turn that caused them with no plumbing. `llm_purpose("classify" | "reply" | "reflect" | "appearance")` labels the model calls made inside a block the same way.
* **Envelope:** `{"seq": int, "ts": float, "type": str, "turn": str | null, "data": {...}}`.
* **Event types:** `turn.start` (`text`), `turn.prompt` (`system_prompt`), `router.keyword`, `router.classified` (`escalate`, `capability`, `reason`, `raw`, `ms`), `router.classify_failed`, `router.route` (`pool`, `wanted_escalation`, `fell_back_to_local`), `llm.call` (`model`, `purpose`, `ms`, token counts, `reasoning_enabled`, `has_reasoning`), `llm.reasoning` (`text`), `llm.error`, `reply.parsed` (`structured`, `say`, `actions`, `raw` when unstructured), `skill.call` (`skill`, `args`, `ok`, `ms`, `result`, `error`), `skill.rejected` (`skill`, `reason`), `turn.reply`, `speech.chunk` (`seq`, `total`, `backend`, `seed`, `synth_s`), `presence.placement`, `reflection.result`, `personality.mood` / `personality.traits` (`values`, `deltas`, `cause`).
* **Privacy:** `turn.prompt` carries the user's chat and the companion's memories. The stream is as exposed as the HTTP port, which is why the daemon and the status page both default to `127.0.0.1`.

---

## Appearance & Blender Orchestration (`brain/appearance/`)

Governed by `docs/DESIGN_AVATAR_EVOLUTION_FLOW.md`:

1. **LookSpec Validation (`spec.py`):** Asserts fields against catalog definitions (`templates_catalog.json`, `wardrobe_catalog.json`). Retries synthesis once on failure before graceful abort. A user-prompted request that exhausts its retry must tell the user so (see `docs/DESIGN_AVATAR_EVOLUTION_FLOW.md`). The acknowledgement returns synchronously and the build runs detached, so this needs a proactive-utterance channel; V1 has none and only logs the failure.
2. **Headless Execution (`blender_gen.py`):** Centralizes background subprocess execution via `asyncio.create_subprocess_exec` with environment sanitization (stripping `VIRTUAL_ENV`, `PYTHONPATH`, `PYTHONHOME`) and a **120-second timeout**:
   ```bash
   blender --background --python blender/generator.py -- --spec <spec> --out <out>
   ```
3. **Subprocess Sandboxing Standard (Accommodation for V2 Skills):** Background script execution utilities standardize on Bubblewrap (`bwrap`) isolation on Linux. If `bwrap` is missing on a host attempting to run sandboxed subprocesses, the execution utility fails fast with `RuntimeError("Bubblewrap ('bwrap') is required for sandboxed skills. Install 'bubblewrap' via your system package manager.")`.
4. **Option B Staging & Atomic Reveal:**
   * If compiled while `User_Away`, asset is staged in `<deploy_dir>/assets/staged/look-v{N}.glb` with `staged_version` recorded in `agent_state`. Live `manifest.json` is untouched.
   * Upon transition to `User_Active`, asset is moved to `<deploy_dir>/assets/look-v{N}.glb`, hardlinked to `data/current/`, committed atomically via `manifest.json`, and revealed conversationally.
   * `compile_and_publish()` is deliberately SQLite-agnostic (`brain/appearance/` has no dependency on `brain/personality/`): it returns a `CompileResult` (`outcome`, `version`, `glb_path`, `manifest`). The glue in `brain/evolution.py` (`AvatarEvolution`) records `agent_state.staged_version`, clears it after `reveal_staged()`, stamps `last_redesign_timestamp`, and reacts to presence transitions. `blender_gen.is_build_running()` exposes the single-build lock's state so eligibility gates can consult it.
   * Old versions beyond `keep_recent_versions` are pruned synchronously as part of publishing. Manifest helpers are pure in-memory operations — `revert_to_version(manifest, target_version)` rewrites the current-version pointer and `mark_version_status()` flags an abandoned version `corrupted` — leaving `load_manifest`/`save_manifest` to the caller.
   * Background work scheduled from `change_appearance()` and `POST /appearance/generate` goes through `AvatarEvolution.schedule_generate_from_full_spec()` / `_schedule_background()` rather than reaching into task sets, and its handlers (`on_presence_transition`, both compile call sites) catch and log exceptions so one bad build never takes down the presence watcher or reflection loop.
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
  * `org.freedesktop.login1.Session` (`LockedHint`, `IdleHint`). The daemon's own session is located via `$XDG_SESSION_ID` and `login1.Manager.GetSession(id)`; `GetSessionByPID` must not be used because it only succeeds for a session's registered leader PID.
  * Signal subscriptions must match on `interface`/`member`/`path` and must not filter on `sender`: the bus honors a well-known-name sender in its routing, but jeepney's local re-check compares against the emitter's unique connection name and would silently drop every signal.
  * Both optional watchers (`ScreenSaver`, `login1`) catch every exception at their top level, log once, and leave the presence default at `User_Active`; the required KWin-facing server task must never be torn down by an optional watcher's failure.
  * `PresenceStateMachine`, `PresenceSignals`, and `PresenceState` live in `desktop_bridge.py`.
* State Machine:
  * **`User_Away`:** Screensaver active, session locked, or display sleeping. `login1`'s `IdleHint` is the available proxy for display sleep (no dedicated signal exists) and contributes to `User_Away`.
  * **`User_Idle`:** No window/pointer activity for $> \text{idle\_after\_s}$ (default 10m). "Activity" is any `UpdateState` call from the sensing adapter. V1 limitation: adapters report window/screen/desktop *changes*, not raw input, so a user reading a static window eventually reads as Idle; true pointer/keyboard activity would require an input-aware source. A dedicated 5-second ticker evaluates the threshold.
  * **`User_Active`:** Active window interaction and unlocked session.

### 2. Non-Intrusive Placement (`placement.py`)
Returns `Placement(x, y, screen_name, hidden)`, with `x`/`y` in global virtual-desktop coordinates and `screen_name` naming the target output:
* **No Active Window:** Bottom-right margin corner of primary monitor.
* **Active Window:** Evaluates display corners in sequence (`bottom-right`, `bottom-left`, `top-right`, `top-left`); selects first with zero collision.
* **Maximized / Colliding:** Migrates to a display chosen by `display_affinity` (`"primary"`, `"secondary"`, `"auto"`).
* **Fullscreen:** Migrates to a display chosen the same way; if single display, emits `hidden = true`.
* **Own-window rejection:** The sensing adapter reports the *active* window, and a focused overlay (e.g. while the input bar is open) is the active window. If that report reaches placement, the avatar treats itself as an obstacle: it flees its own corner, its new window is reported, it flees back, and it flaps between corners indefinitely. The adapter filters its own window by name, but a layer-shell surface's reported class is not guaranteed, so the daemon must not depend on that alone: telemetry whose window matches the avatar's last-placed rectangle (same position within a few pixels, same size as `avatar_width_px` × `avatar_height_px`) is discarded, without updating the retained last-known presence state. Matching on geometry rather than identity is what makes this robust.
* **Affinity resolution:** The sensing payload never flags a screen as primary, so `screens[0]` (list order) is treated as primary. If the preferred target is the very screen being escaped, any other screen is used. Returned coordinates are in **global virtual-desktop space** and `screen_name` identifies the target output; clients are responsible for converting to output-relative offsets (see `docs/SPEC_OVERLAY.md`).

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
          "presence": {"avatar_width_px": 240, "avatar_height_px": 240, "show_speech_bubble": true},
          "shortcuts": {"toggle_interaction": "Meta+Alt+D", "push_to_talk": "Meta+Alt+M", "push_to_talk_mode": "hold"}
        }
      }
    }
    ```
  * `welcome.manifest` is a wire projection of the on-disk manifest that omits internal-only build fields (`seed`, `evaluated_triangles`, `size_bytes`) and, as with `hot_reload_asset`, shares one projection helper; `look_spec` and `warnings` appear only in the on-disk manifest and are stripped on both wire events. `welcome.config` is a hand-built subset (`avatar_width_px`, `avatar_height_px`, the full `shortcuts` block, and `voice.volume` from `[voice.tts] volume`) — brain-internal presence fields (`dbus_service_name`, `idle_after_s`, `margin_px`, `display_affinity`, …) are never sent.
* **Client ID Collision:** Drops older socket with close code `4409` (`Client ID Replaced`).
* **Handshake Failures:** A `protocol_version` mismatch, a non-`hello` first message, or a malformed `hello` payload closes the socket. Origin/Host rejection happens before `accept()`.
* **Initial Placement:** Sensing telemetry only arrives when a window changes, so the daemon retains the last window/screen state and, immediately after sending `welcome` to a (re)connecting client, broadcasts the current `set_position` (`on_client_connected`). Without this a freshly started or reconnected overlay would stay at its default margin until the user next switched windows. If no telemetry has arrived yet nothing is sent.
* **Post-Handshake Tolerance:** An unrecognized event type, or a recognized type with a malformed payload, logs a warning and leaves the connection open — one bad message never ends the session. `audio_stream_chunk.seq` is parsed but not enforced against a counter (the following binary frame is trusted; ordering is guaranteed by the single TCP stream).
* **Hub Scope:** `OverlayHub.handle_connection` is mounted at `/ws/overlay` by the FastAPI app built in `brain/main.py`; the hub owns no app of its own. `OverlayHub.broadcast()` prunes sockets that fail on send.

### 2. Standardized Event Payload Schemas

```json
// Daemon -> Client
{"type": "chat_response", "timestamp": 1718000000.0, "payload": {"utterance_id": "utt_1", "text": "Hello!", "provider": "local", "escalated": false, "rationale": "chat"}}
{"type": "set_position", "timestamp": 1718000000.0, "payload": {"x": 100, "y": 200, "screen_name": "DP-1", "duration_ms": 400, "hidden": false}}
{"type": "status_update", "timestamp": 1718000000.0, "payload": {"state": "idle" | "thinking" | "rebuilding" | "listening" | "speaking"}}
{"type": "mood_update", "timestamp": 1718000000.0, "payload": {"energy": 60.0, "valence": 50.0, "stress": 20.0}}
{"type": "play_gesture", "timestamp": 1718000000.0, "payload": {"clip": "greet", "loop": false}}
{"type": "speech", "timestamp": 1718000000.0, "payload": {"utterance_id": "utt_1", "chunk_seq": 0, "final": true, "wav_path": "...", "audio_url": "/audio/utt_1.wav", "text": "Hello!", "delivery": "animated", "visemes": []}}
{"type": "config_update", "timestamp": 1718000000.0, "payload": {"presence": {}, "shortcuts": {}, "voice": {"volume": 1.0}}}

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
{"type": "asset_swap_result", "timestamp": 1718000000.0, "payload": {"ok": true, "version": 3, "error": null}}

// Reserved V2+ Sub-Agent Permission Response (Client -> Daemon):
{"type": "agent_permission_response", "timestamp": 1718000000.0, "payload": {"task_id": "t_1", "decision": "allow_once" | "always_allow" | "deny"}}
```

### 3. Administrative REST Surface (`brain/main.py`)

The daemon exposes an asynchronous FastAPI HTTP service. FastAPI automatically synthesizes and serves interactive OpenAPI 3.1 specifications (`/openapi.json` and `/docs`) directly from Pydantic models.

The administrative surface lives in `brain/main.py`; the WebSocket hub in `brain/ipc/server.py` is mounted into the same app. The primary administrative endpoints and concrete wire payloads include:

#### `GET /health`
* **Status:** `200 OK`
* **Response Body:**
  ```json
  {
    "status": "ok",
    "name": "Deskmate",
    "active_voice": "piper:en_US-lessac",   // derived from the live TTS config: "piper:<stem>" or "remote:<name>"
    "voice_degraded": false,                // true when remote TTS is configured but every instance is cooling down
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
* **Behavior:** Processes dialogue through the router, splits the reply into dialogue and skill calls (see "Structured Replies & Skills"; only the dialogue is spoken, stored and broadcast as `chat_response.text`), logs to SQLite `interactions`, broadcasts `chat_response`, and emits `speech` (with synthesized audio and visemes) over WebSocket.

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

#### `GET /api/status`
* Read-only runtime state that exists only in the daemon's memory, for the status page: `started_at`, `overlay_clients`, `interaction_active`, `build_running`, `placement` (`x`, `y`, `screen`, `hidden`), the last active `window` and `screens`, and `look` (`current_version`, `versions`, `summary`).

#### `GET /api/skills`
* Read-only view of the skill registry: `enabled` (the `[skills]` switch), `max_actions_per_reply`, `skills` (each with `name`, `enabled`, `signature`, `description`, `params` (`name`, `description`, `enum`, `required`) and in-memory counters `calls`, `failures`, `refused`, `last_used`, `last_error`), and `prompt_block`, the exact text appended to the system prompt.

#### `GET /api/events`
* A server-sent-event stream (`text/event-stream`) of what the daemon is doing; see "Event Stream (`brain/events.py`)" below. `?since=<seq>` (or a `Last-Event-ID` header) replays the retained backlog after that sequence number, then follows live. A comment keepalive is sent every 15 s.

#### Streaming & Asset Endpoints:
* `GET /audio/{utterance_id}.wav` $\to$ Streams temporary utterance audio (`audio/wav`).
* `GET /assets/{filename}` $\to$ Streams runtime `.glb` models from `data/current/` (`model/gltf-binary`).
* `WS /ws/overlay` $\to$ Presentation WebSocket hub.

#### Reserved V2 Administrative Endpoints (contract only — not implemented in V1; V2 accommodation means reserved keys and fields, never code):
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
* **`test_voice_select.py`:** Asserts look-to-group mapping (explicit base versus `femininity`, robot fallback), stable persistence of the chosen seed, re-selection on a body-type change with `previous_voice_id` recorded, the no-selection cases (local backend, empty pools, non-`tts_serve` provider), and that per-instance params do not leak to a failover instance.
* **`test_voice.py`:** Asserts per-call overrides on `synthesize()`, STT/TTS failovers, and viseme timing calculations.
* **`test_appearance.py`:** Asserts LookSpec validation, 120s timeout handling, Option B staged holding, atomic manifest commits, and Bubblewrap missing-binary detection.
* **`test_placement.py` / `test_desktop_bridge.py`:** Assert corner placement logic, `display_affinity` migration, and D-Bus logind/screensaver telemetry parsing. The bridge tests run against real session/system bus services where available; assertions about overall presence state must not depend on the host's real, independently changing lock state (assert on the raw signal flag being tested instead).
* **`test_ipc.py`:** Tests Origin/Host validation, client handshake (`hello` $\to$ `welcome`), duplicate `client_id` closure (`4409`), dual-delivery payload generation, and unhandled-event graceful passthrough for reserved agent task/permission envelopes (`plan_id` verification). Post-`accept()` rejections are observable to Starlette's `TestClient` only on the next send/receive, so such tests follow the connect with an explicit receive inside the `pytest.raises` block. `OverlayHub.broadcast()` is exercised against duck-typed fake sockets because a sync test cannot schedule onto a live socket's event loop.
* **`test_skills.py`:** Asserts the reply parser's forgiving-in-one-direction rule (fenced/chatty JSON parses; missing `say`, code samples and plain text never become actions), registry validation (unknown skill, bad enum, extra/missing/oversized arguments), the per-reply action cap, and that one failing skill does not stop the others.
* **`test_skills.py` (registry view):** `describe()` lists every skill, enabled or not, with usage counters.
* **`test_events.py`:** Asserts the bounded backlog, `since` replay, subscriber delivery, turn-id propagation through awaits, that `emit()` never raises, and mood-history recording (nudges recorded, tiny decay not).
* **`test_plaintext.py`:** Asserts Markdown/emoji stripping, vocal-cue mapping, silent-action dropping, and that plain text and identifiers (`snake_case`, `3:30`) survive.
* **`test_status_page.py`:** Asserts the Host-header guard, log-source whitelisting (names only, never paths), and read-only database access.
* **`test_main.py`:** Exercises the full app state with the LLM faked via `httpx.MockTransport` and TTS faked by a backend writing a valid silent WAV, while deliberately running the real `rhubarb` binary for lipsync (fast and deterministic against silence).
* **`test_evolution.py`, `test_manifest.py`, `test_reflect.py`, `test_prompts.py`, `test_tts_pool.py`, `test_lipsync.py`, `test_llm_pool.py`:** Cover the staging/reveal glue (with only the Blender step faked — real manifest and file operations run), manifest versioning and pure rollback helpers, reflection clamping and result shape, prompt ranking, TTS pool priority and `active_backend` handling, viseme mapping, and pool cooldown/capability behavior.