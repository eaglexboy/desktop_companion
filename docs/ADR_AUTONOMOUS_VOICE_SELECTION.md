# Architecture Decision Record: Autonomous Voice Selection & Voice Cloning

* **Status:** Accepted for V2+ Roadmap (Deferred from V1; V1 Interface Accommodations Approved)

* **Date:** 2026-09-25

* **Deciders:** Architecture & Core Engineering Team

* **Supersedes:** Initial Brain Voice Design Notes

## 1. Context & Executive Recommendation

The Deskmate companion features a procedurally evolving visual appearance governed by `docs/DESIGN_AVATAR_EVOLUTION_FLOW.md`. Users desire acoustic counterpart capabilities: allowing the agent to dynamically adopt preset voice personas (e.g., warmer, more formal, eccentric) or clone voices from clean reference samples.

### Key Decisions:

1. **Defer Implementation to V2+:** Prioritize core low-latency desktop STT, TTS, and phoneme-to-viseme lip-sync reliability for V1.

2. **Coupled Evolution (User-Configured Default, Agent-Chosen Evolution):** The user configures the baseline default voice. When the companion triggers an autonomous redesign during its reflection loop, it evaluates whether to evolve **only its visual look** or **both its visual look and voice together**.

3. **Four Non-Breaking V1 Accommodations:** Apply lightweight accommodations to the V1 `SPEC_BRAIN.md` contracts so V2 requires zero breaking rewrites: per-call voice overrides, SQLite `agent_state` with `staged_voice_id`, state inspection in `GET /health`, and configuration catalog stubs.

## 2. Decoupling Configuration from Runtime Agent State

In V1, `config.toml` is user-authored and dynamically watched via filesystem notifications (`watchdog`). The daemon must **never overwrite or modify `config.toml`** at runtime.

* **`config.toml` Ownership:** Defines the default bootstrap voice paths, fallback engines, and the catalog of available preset voices (`[voice.voice_catalog.<id>]`).

* **Catalog Schema Specification (`config.toml`):**

  ```
  [voice.voice_catalog.piper_en_lessac]
  backend = "local_piper"
  model_path = "voices/en_US-lessac-medium.onnx"
  config_path = "voices/en_US-lessac-medium.onnx.json"
  display_name = "Lessac"
  gender_presentation = "female"
  traits = { formality = 75, extraversion = 50, affection = 65 }
  tags = ["articulate", "clear", "calm"]
  
  [voice.voice_catalog.remote_echo]
  backend = "remote_http"
  voice_id = "echo"
  display_name = "Echo"
  gender_presentation = "neutral"
  traits = { formality = 40, boldness = 70, humor = 75 }
  tags = ["animated", "casual", "playful"]
  
  ```

* **Persistent Runtime State (`state.db`):** The companion's active and staged voice selections are stored in SQLite under an `agent_state` table (`current_voice_id`, `previous_voice_id`, `staged_voice_id`, `voice_pinned`). No duplicate `voices` table is used; presets are owned authoritatively by `config.toml`.

* **Canonical Voice Identifier Namespaces:**

  * Local Piper models: `piper:<model_stem>` (resolved relative to `[voice.tts] voices_dir`).

  * Remote HTTP voice IDs: `remote:<voice_id>`.

* **Config vs. State Precedence:**

  * `agent_state.current_voice_id` takes precedence over `config.toml` only when explicitly set or evolved.

  * In V1, `current_voice_id` is initialized to `null`, meaning the active voice strictly tracks `config.toml` edits.

* **User Pinning:** If a user specifies *"Keep this voice"* or *"Don't change your voice"*, `voice_pinned = true` is set in `agent_state`, suppressing autonomous acoustic mutations while permitting visual evolution.

## 3. Acoustic Trait Mapping & Autonomous Selection Algorithm

Voice changes must reflect stable persona traits rather than transient mood fluctuations:

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Acoustic Decision Separation                                            │
├───────────────────────────────────┬─────────────────────────────────────┤
│ Stable Traits (0–100)             │ Volatile Mood Axes (0–100, Half-Life│
│ Determines: Voice Persona Model   │ Determines: Per-Utterance Modulation│
├───────────────────────────────────┼─────────────────────────────────────┤
│ • Extraversion / Boldness (≥ 70)  │ • High Energy: Speed rate 1.05–1.15 │
│   → Animated, colorful voice IDs  │ • Low Energy: Subdued, relaxed pitch│
│ • Formality (≥ 70)                │ • High Stress: Sharper cadence,     │
│   → Measured, crisp articulation  │   tighter delivery                  │
│ • Affection (≥ 70)                │                                     │
│   → Warm, resonant vocal tones    │                                     │
└───────────────────────────────────┴─────────────────────────────────────┘

```

### Deterministic Voice Selection Algorithm (V2 Reflection Cycle):

When reflection evaluates an autonomous evolution pass:

1. **Pinning & Gate Check:** If `voice_pinned == true`, voice selection is skipped.

2. **Coupled Choice Roll:** The agent evaluates `autonomous_redesign_probability`. If triggered, it rolls whether to evolve appearance only ($50\%$) or coupled appearance + voice ($50\%$).

3. **Trait Affinity Scoring:** For each candidate $V$ in `[voice.voice_catalog]`:
   

   $$
   \text{Score}(V) = -\sum_{t \in \text{traits}} w_t \cdot \left\vert{} T_{\text{agent}}(t) - T_V(t) \right\vert{} + \text{AffinityBonus}(V, \text{LookSpec})
   $$

   * $T_{\text{agent}}(t)$ is the agent's current stable trait value ($0\text{–}100$).

   * $T_V(t)$ is the catalog entry's designated baseline trait score.

   * $\text{AffinityBonus}$ adds weight for matching aesthetic styles (e.g., robotic/synth voice for mechanical base templates).

4. **Exclusion:** The currently active `current_voice_id` is excluded to guarantee that a triggered evolution represents an actual change.

5. **Selection:** The candidate with the highest score is selected.

## 4. Operational Mechanics & Safety Invariants

1. **Utterance Boundary Invariant:** A voice transition takes effect exclusively at the next utterance boundary; the pipeline never swaps voices mid-sentence.

2. **Coupled Presence-Aware Staging & Reveal:**

   * If evaluated while the user is away (`User_Away`), the chosen voice ID is written to `agent_state.staged_voice_id`. **The live active voice is not modified.**

   * At the moment the user returns (`User_Active`), `staged_voice_id` is atomically committed into `current_voice_id` at the exact same commit point as the appearance reveal in `docs/DESIGN_AVATAR_EVOLUTION_FLOW.md`.

   * The companion greets the user in its new voice and explicitly mentions both changes (e.g., *"Welcome back! I decided to try out a new look and voice. What do you think?"*).

   * **Internal Daemon Lifecycle:** Staging and commits are internal lifecycle events driven by `reflect.py` and `desktop_bridge.py`. External RPC endpoints for `/voice/stage` or `/voice/commit` are rejected by design to maintain autonomous persona encapsulation.

3. **Coupled Cooldown:** Autonomous voice evolution shares the mandatory **12–24 hour cooldown** (`redesign_cooldown_hours_min/max`) with appearance evolution. User-prompted voice changes reset the shared cooldown.

4. **Degradation, Timeouts & Fallback Logging:**

   * Remote TTS calls enforce timeout `[voice.tts.remote.timeout_s]` (default $10.0\,\text{s}$).

   * If a selected remote voice server times out or returns a 5xx response, synthesis falls back immediately for that turn to the previous active voice, and ultimately to local Piper `default.onnx`.

   * On fallback, the daemon logs a warning:

     ```
     WARNING: Remote TTS failed (<reason>); falling back to local piper:default.onnx
     
     ```

   * An in-memory flag `voice_degraded = true` is set, observable via `GET /health`.

   * The pipeline derives viseme frames strictly from the **resulting synthesized audio waveform**, guaranteeing lip-sync alignment regardless of voice duration differences.

5. **Revert & Undo:** If the user commands *"Go back to your old voice"*, the daemon reads `previous_voice_id` from `agent_state` and restores it immediately.

6. **Administrative HTTP Endpoints:**
   To support manual testing, UI overrides, and administrative scripting, the daemon provides:

   * `POST /voice/pin`: Body `{"pinned": bool}`. Updates `agent_state.voice_pinned` to freeze or unfreeze autonomous voice mutations. Returns `200 {"status": "ok", "voice_pinned": bool}`.

   * `POST /voice/revert`: Immediately restores `previous_voice_id` into `current_voice_id`. Returns `200 {"status": "ok", "active_voice": str}`.

   * `GET /voice/catalog`: Returns the active in-memory catalog parsed from `config.toml`:

     ```json
     {
       "current_voice_id": "piper:en_US-lessac",
       "voice_pinned": false,
       "voices": {
         "piper_en_lessac": {
           "backend": "local_piper",
           "display_name": "Lessac",
           "gender_presentation": "female",
           "traits": { "formality": 75.0, "extraversion": 50.0, "affection": 65.0 },
           "tags": ["articulate", "clear", "calm"]
         }
       }
     }
     
     ```

## 5. Voice Cloning Policy: Consent, Provenance & Privacy

Zero-shot neural cloning (XTTS-v2, F5-TTS, CosyVoice) introduces privacy and impersonation risks that require strict structural bounds:

* **User-Curated Storage:** The companion discovers cloning reference samples **only** within `<deploy_dir>/assets/voices/reference_samples/`.

* **Sample Audio Standards:** Reference samples must be validated: 16–24 kHz mono WAV, duration between 3.0 and 10.0 seconds, meeting minimum loudness and SNR thresholds.

* **Prohibition on Autonomous Capture:** The agent is programmatically barred from recording the user or third parties to synthesize a clone, and cannot scrape audio from web sources.

* **Consent Storage & Local-First Privacy:**

  * Zero-shot voice cloning defaults to local or private-network inference backends.

  * Transmitting audio reference samples to cloud TTS APIs requires explicit opt-in via `allow_cloud_cloning = true` stored in `config.toml` under `[voice.tts]`.

  * If `allow_cloud_cloning = false` (default), any attempt to synthesize cloned voices against a remote/cloud backend immediately aborts and logs an authorization warning.

  * For remote backends that do not share a filesystem, sample audio bytes are transmitted via multipart POST or uploaded once and referenced by sample ID.

* **Provenance Tracking:** Every cloned voice record stores metadata (`clone_source`, `licence`, `sample_hash`).

## 6. The Four V1 Micro-Accommodations

To eliminate future refactoring friction without adding V2 scope to V1, the following lightweight adjustments are approved for `SPEC_BRAIN.md`:

1. **Per-Call Voice Signature in `TTSBackend`:**

   ```python
   class TTSBackend(ABC):
       @abstractmethod
       async def synthesize(
           self, 
           text: str, 
           out_path: str, 
           *, 
           voice_id: str | None = None, 
           params: dict | None = None
       ) -> str:
           """Synthesizes text to a target WAV file with optional runtime voice overrides."""
           pass
   
   ```

2. **SQLite `agent_state` Staging Columns:**
   Include `current_voice_id`, `previous_voice_id`, `staged_voice_id`, `voice_pinned`, `staged_version`, and `last_redesign_timestamp` in `state.py`.

3. **State Inspection in `GET /health`:**
   Include `"active_voice": str` and `"voice_degraded": bool` alongside traits and mood in the administrative `/health` response. Example response payload:

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

4. **Catalog Stubs in Configuration:**
   Reserve commented-out `[voice.voice_catalog.<id>]` placeholders in `config.example.toml`.