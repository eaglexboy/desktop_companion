# Architecture & Flow: Avatar Appearance Evolution

## 1. Overview & Core Philosophy

The avatar's visual appearance is dynamic, reflecting both explicit user requests and the companion's emergent personality and mood state. However, visual changes must adhere to three foundational tenets:

1. **Non-Disruptive Execution:** 3D asset compilation takes several seconds in headless Blender; generation must run completely in the background without blocking conversation, audio processing, presence monitoring, or window management.
2. **Coherence & Identity Continuity:** The companion must maintain a stable persona. Autonomous redesigns propose **deltas relative to the current LookSpec** (e.g., swapping jackets, adjusting palettes, putting on glasses) rather than randomly reincarnating into an unrelated species or archetype.
3. **Presence-Aware Timing (Option B: Staged in Secret, Revealed on Return):** The agent never speaks to an empty room or silently swaps assets while the user is away. If a redesign is compiled while the user is away or screens are locked, the asset is staged in `<deploy_dir>/assets/staged/` and only committed and revealed conversationally the moment the user returns.

---

## 2. Redesign Triggers & Decision Architecture

```
                         ┌─────────────────────────────────────────────────┐
                         │               Appearance Triggers               │
                         └───────────────────────┬─────────────────────────┘
                                                 │
                     ┌───────────────────────────┴───────────────────────────┐
                     ▼                                                       ▼
      ┌─────────────────────────────┐                         ┌─────────────────────────────┐
      │   Trigger A: User-Prompted  │                         │ Trigger B: Autonomous Mood  │
      │   (Explicit / Suggestive)   │                         │  ("When it feels like it")  │
      └──────────────┬──────────────┘                         └──────────────┬──────────────┘
                     │                                                       │
                     │ • "Change your look"                                  │ • Evaluated in reflect.py pass
                     │ • "Wear a wizard robe"                                │ • Cooldown: 12–24h elapsed
                     │ • "Put on cool glasses"                               │ • Probability roll (50% default)
                     │ • Resets autonomous cooldown                          │ • Traits bias what changes
                     └───────────────────────────┬───────────────────────────┘
                                                 │
                                                 ▼
                               ┌───────────────────────────────────┐
                               │ Dedicated Delta Synthesis LLM Call│
                               │ (Structured JSON via local tier)  │
                               └─────────────────┬─────────────────┘
                                                 │
                                                 ▼
                               ┌───────────────────────────────────┐
                               │ Schema Validation (brain/spec.py) │
                               │ (1 retry with feedback on failure)│
                               └─────────────────┬─────────────────┘
                                                 │
                                                 ▼
                               ┌───────────────────────────────────┐
                               │ Background Subprocess Compilation │
                               │ (blender/generator.py in scratch) │
                               │ Emits status_update: "rebuilding" │
                               └─────────────────┬─────────────────┘
                                                 │
                                                 ▼
                               ┌───────────────────────────────────┐
                               │ Presence-Aware Staging / Reveal   │
                               │ If User_Away: stage in staged/    │
                               │ If User_Active: commit & reveal   │
                               └─────────────────┬─────────────────┘
                                                 │
                                                 ▼
                               ┌───────────────────────────────────┐
                               │ Atomic Commit Point (Assets &     │
                               │ manifest.json updated atomically) │
                               └─────────────────┬─────────────────┘
                                                 │
                                                 ▼
                               ┌───────────────────────────────────┐
                               │ WebSocket hot_reload_asset Event  │
                               │ Overlay swaps mesh & sends ack    │
                               │ (asset_swap_result ok / fail)     │
                               └───────────────────────────────────┘
```

---

### Trigger A: User-Prompted (Direct or Suggestive)

When the user asks or invites the companion to modify its look:

* **Direct Commands:** *"Change into something warm,"* *"Put on neon glasses,"* *"Can you look like a mechanical bot?"*
* **Open Invitations:** *"If you want to change your outfit, go ahead,"* *"You can change your look whenever you'd like."*

#### Handling Mechanism:
1. **Intent Classification:** The dialogue router detects an `appearance_change` intent (via tool call `change_appearance(request: str)` or a fast classification pre-filter in `router.py`).
2. **Immediate Conversational Reply:** The companion immediately responds conversationally without waiting for 3D synthesis: *"I like that idea. Let me put something together..."*
3. **Dedicated Delta Synthesis:** A non-conversational worker invocation is dispatched to the local LLM tier passing `{current_look_spec, catalogs, trigger_context}` to generate a structured `LookSpec` delta.
4. **Immediate Reveal:** Because the user is present and actively interacting, the build is committed and hot-swapped as soon as compilation finishes (no presence staging).
5. **Cooldown Reset:** A successful user-prompted redesign resets the autonomous evolution cooldown timer, preventing an autonomous change from firing shortly after a user-directed one.

---

### Trigger B: Autonomous Evolution ("When It Feels Like It")

The companion can autonomously decide to evolve its look during the periodic **Reflection Cycle** (`brain/personality/reflect.py`, evaluated every `reflection_interval_s = 3600`).

#### 1. Deterministic Eligibility & Probability Gate
An autonomous redesign pass is evaluated **only** when all of the following conditions are met:
* The autonomous cooldown window has fully elapsed ($12\text{–}24\text{ hours}$ sampled uniformly from `redesign_cooldown_hours_min` and `redesign_cooldown_hours_max`).
* No compilation subprocess is currently running (concurrency lock is free).
* The user is not currently in **Active Interaction Mode** (input bar closed, microphone inactive).
* **Desire Roll:** When eligible, a roll against `autonomous_redesign_probability` (default `0.5`) determines whether a redesign is triggered.

#### 2. Personality & Trait Biases (Biasing *What* Changes, Not Gating *If*)
Because personality traits begin at neutral ($50$) and move gradually ($\le \pm 5$ per cycle), traits act as stylistic biases rather than blocking gates:
* **Openness:** High Openness ($\ge 70$) biases toward trying new wardrobe styles, accessory sockets, or alternate base templates. Low Openness ($\le 30$) favors slight palette tweaks or subtle accessory shifts.
* **Boldness:** High Boldness ($\ge 70$) favors vibrant, saturated palettes and striking, expressive accessories.
* **Formality:** High Formality ($\ge 70$) favors structured, tailored outfits (e.g., lab coat, vest) over casual or whimsical styles.
* **Mood Influence (Per-Utterance & Trend):** Sustained shifts on the volatile mood axes (`energy`, `valence`, `stress`) influence stylistic choices (e.g., high energy prompts brighter accents; elevated stress prompts cozy, relaxed attire).

#### 3. Delta-from-Current Constraint (Change Budget)
Autonomous proposals must be formatted as **deltas against the active LookSpec**, enforcing character coherence:
* At most $1\text{–}3$ attributes may change per autonomous cycle (e.g., changing footwear and adding glasses, or changing primary and accent palette).
* **Base Template Anchor:** The underlying `base_template` cannot be altered autonomously unless `openness >= 85` and the companion has maintained its previous base for at least 7 days (`base_template_since` in `agent_state`).

---

## 3. Presence Telemetry & The "Welcome Back" Reveal (Option B)

Visual reveals are strictly coordinated with desktop presence telemetry:

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Desktop Presence States (brain/presence/desktop_bridge.py)              │
├─────────────────┬───────────────────────────────────────────────────────┤
│ User_Active     │ Desktop awake, active window moving, input recent.    │
│ User_Idle       │ Desktop awake, but zero activity > idle_after_s (10m).│
│ User_Away       │ Screensaver active, screen locked, or DPMS sleeping.  │
└─────────────────┴───────────────────────────────────────────────────────┘
```

### Telemetry Data Sources (`desktop_bridge.py`):
KWin geometry scripts cannot observe global idle timers or screen lock states. Therefore, `desktop_bridge.py` subscribes directly to system and session D-Bus signals:
* `org.freedesktop.ScreenSaver` / `org.kde.screensaver` (`ActiveChanged` signal).
* `org.freedesktop.login1.Session` (`LockedHint` and `IdleHint` properties).
* Inactivity timer evaluating time elapsed since last window geometry change or input event against `[presence] idle_after_s = 600` (10 minutes).

### The Staged Reveal Lifecycle:

```
[User Active] ──────► [User Leaves / Screen Locks (User_Away)]
                                    │
                                    ▼
               Autonomous Cooldown Expires in reflect.py
                                    │
                                    ▼
               Background Build Compiles Headlessly in Scratch
               Staged: <deploy_dir>/assets/staged/look-v{N}.glb
               Recorded: agent_state.staged_version = N
               (NO LIVE SWAP, NO MANIFEST CHANGE, NO SPEECH)
                                    │
                                    ▼
[User Returns / Unlocks Screen (User_Away or User_Idle ──► User_Active)]
                                    │
                                    ▼
               1. Move staged glb to assets/ & data/current/
               2. Atomic manifest commit (manifest.json updated)
               3. Clear agent_state.staged_version
               4. Broadcast hot_reload_asset to Overlay
               5. Overlay cross-fades mesh cleanly
               6. Companion delivers verbal reveal greeting:
                  "Welcome back! While you were away, I decided to try
                   out a new look. What do you think of this jacket?"
```

1. **Compilation While Away:** If an autonomous redesign is triggered while `User_Away` or `User_Idle`, Blender compiles the new `.glb` silently in the background.
2. **Staged Holding Area:** The compiled asset is moved to `<deploy_dir>/assets/staged/look-v{N}.glb` and recorded in `agent_state.staged_version`. The live directory `<deploy_dir>/data/current/` and `manifest.json` are **not touched**, and no WebSocket events or audio are emitted.
3. **The Reveal on Return:** When `desktop_bridge.py` detects a transition from `User_Away` or `User_Idle` $\to$ `User_Active`:
   * The daemon executes the atomic asset commit.
   * `last_redesign_timestamp` is stamped in `agent_state` at this reveal moment.
   * Emits `hot_reload_asset` over WebSocket.
   * Upon client swap confirmation, the companion greets the user conversationally.
4. **Boot-Time Crash Reconciliation:** If the daemon starts with `staged_version` non-null:
   * It asserts that `<deploy_dir>/assets/staged/look-v{N}.glb` exists (clearing `staged_version` to `null` if missing).
   * **It never reveals immediately on startup.** It preserves the staged asset and awaits an observed live transition from `User_Away`/`User_Idle` $\to$ `User_Active`.

---

## 4. End-to-End Pipeline & Atomic Commit Sequence

```
User / Reflection Loop          Brain Daemon                Blender Subprocess          Overlay Client
        │                            │                             │                          │
        │── Trigger appearance ─────►│                             │                          │
        │                            │── status_update: rebuilding ──────────────────────────►│
        │                            │   (Suppressed if User_Away) │                          │
        │                            │── Spawn headless build ────►│                          │
        │                            │   (asyncio exec in scratch) │                          │
        │                            │                             │                          │
        │   [Daemon loops and chat continue completely unaffected] │                          │
        │                            │                             │                          │
        │                            │◄── Exit 0 + .build.json ────│                          │
        │                            │                             │                          │
        │                            │── 1. Validate build schema  │                          │
        │                            │   [If User_Away: Stage]     │                          │
        │                            │   [If User_Active: Commit]  │                          │
        │                            │── 2. Stage glb in assets/   │                          │
        │                            │── 3. Hardlink data/current/ │                          │
        │                            │── 4. Atomic manifest commit │                          │
        │                            │                             │                          │
        │                            │── hot_reload_asset ───────────────────────────────────►│
        │                            │                             │                          │
        │                            │                             │    [Asynchronously loads]│
        │                            │                             │    [Cross-fades mesh]    │
        │                            │                             │    [Binds morph targets] │
        │                            │                             │                          │
        │                            │◄── asset_swap_result {ok} ─────────────────────────────│
        │                            │                             │                          │
        │                            │── Prune assets & data/current                          │
        │                            │── status_update: idle ────────────────────────────────►│
        │                            │── Verbal reveal speech ───────────────────────────────►│
```

### Detailed Pipeline Steps:

1. **Delta Synthesis & Schema Validation (`brain/appearance/spec.py`):**
   * A dedicated structured call takes `{current_look_spec, catalogs, trigger_context}` and emits a candidate JSON delta.
   * `spec.py` enforces validation using **Pydantic v2** models referencing `examples/sample_look.json` and catalog bounds (`templates_catalog.json` and `wardrobe_catalog.json`).
   * **Candidate Delta Example Emitted by LLM:**
     ```json
     {
       "wardrobe": {
         "outfit": "wizard_robe"
       },
       "accessories": {
         "socket_eyes": "glasses",
         "socket_head_top": "pointy_hat"
       },
       "palette": {
         "primary": "#1E1E2E",
         "accent": "#F38BA8"
       }
     }
     ```
   * **Pre-Compile Retry Loop:** If validation fails, the generator retries **1 immediate time** by feeding the exact schema validation error back to the synthesizer model. If it fails a second time:
     * Autonomous passes fail silently without consuming cooldown.
     * User-prompted requests yield: *"I couldn't quite figure out how to make that change. Could you try describing it differently?"*

2. **Scratch Isolation & Blender Headless Execution (`brain/appearance/blender_gen.py`):**
   * Validated merged LookSpec JSON is written to `workspace_dir`.
   * Blender is spawned headlessly with `--factory-startup` to guarantee a clean, isolated environment unaffected by user preferences, startup files, or third-party addons installed in `~/.config/blender/`:
     ```bash
     blender --background --factory-startup --python blender/generator.py -- --spec <spec_path> --out <out_path>
     ```
   * `blender_gen.py` executes the subprocess with a **120-second timeout**, scrubbing virtual environment variables (`VIRTUAL_ENV`, `PYTHONPATH`, `PYTHONHOME`) and purging active venv paths from `PATH`.
   * Emits `status_update: {"state": "rebuilding"}` (suppressed if `User_Away`).

3. **Ground-Truth Verification (`<out>.build.json`):**
   * Inspects build output: glTF binary magic bytes (`0x46546C67`), valid `<out>.build.json`, all 9 canonical Rhubarb shape keys (if `has_face == true`), and core clips (`idle_*`, `move`, `think`).
   * If verification fails, build is aborted, error logged, and live assets remain untouched.

4. **Same-Filesystem Atomic Commit (`EXDEV` Guard & Manifest Commit Point):**
   * In POSIX systems, calling `os.replace` or `os.rename` across distinct filesystems (e.g., from NVMe scratch `workspace_dir` to standard `/home` or NAS `deploy_dir`) raises `OSError: [Errno 18] Invalid cross-device link` (`EXDEV`).
   * **The Invariant:** All staging temporary files (`.tmp`) used for atomic rename must be written directly inside the target directory within `deploy_dir` before calling `os.replace()`.
   * **Standard Atomic Write Pattern:**
     ```python
     def atomic_write(target_path: Path, data: bytes | str) -> None:
         """Atomically writes data to target_path using a same-directory temp file."""
         temp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
         mode = "wb" if isinstance(data, bytes) else "w"
         encoding = None if isinstance(data, bytes) else "utf-8"
         with open(temp_path, mode, encoding=encoding) as f:
             f.write(data)
             f.flush()
             os.fsync(f.fileno())
         os.replace(temp_path, target_path)
     ```
   * **Step 1:** Copy binary from `workspace_dir` into `<deploy_dir>/assets/look-v{N}.glb.tmp`, flush and sync, and `os.replace` to `look-v{N}.glb`.
   * **Step 2:** Copy/hardlink into `<deploy_dir>/data/current/look-v{N}.glb.tmp`, flush and sync, and `os.replace` to `look-v{N}.glb`.
   * **Step 3 (The Manifest Commit Point):** Serialize updated manifest to `<deploy_dir>/manifest.json.tmp`, flush and sync, and `os.replace` to `manifest.json`.

5. **IPC Notification & Client Cross-Fade:**
   * Daemon emits `hot_reload_asset` with payload `{"manifest": AssetManifest, "glb_url": "/assets/look-v{N}.glb"}`.
   * Presentation client asynchronously loads the asset, cross-fades mesh, and returns `asset_swap_result`.

6. **Pruning & Finalization:**
   * Upon receiving `asset_swap_result: {ok: true}` (or 60-second grace timeout), daemon prunes `<deploy_dir>/assets/` and old versions in `<deploy_dir>/data/current/` exceeding `keep_recent_versions` (default 3).
   * Scratch artifacts are cleaned; daemon emits `status_update: {"state": "idle"}`.

---

## 5. Failure Handling, Rollbacks & User Reverts

### 1. Client Swap Failure & Automatic Rollback
If the overlay client encounters an OpenGL shader crash or asset failure, it transmits `asset_swap_result: {"ok": false, "error": "..."}`:
* Daemon immediately reverts `manifest.json` to version $N-1$, marks version $N$ as corrupted, and broadcasts `hot_reload_asset` for the old version.
* If version 1 fails on a fresh install, daemon falls back to "no avatar/placeholder" mode and logs a critical error without crashing.

### 2. Fast User Undo / Revert ("Go back to your old look")
Users can ask to revert at any time:
* Router detects intent via `revert_appearance()` tool or keyword classifier. Also exposed via `POST /appearance/revert`.
* Reverting requires **zero Blender recompilation** because recent `.glb` files and LookSpecs are preserved.

### 3. Concurrency & Collision Rules
* Only one Blender compilation subprocess runs at a time.
* If an autonomous build is running and the user requests a redesign, the autonomous build is cancelled (`SIGTERM`), scratch cleaned, and the user request is prioritized.
* User request queue depth is limited to 1 (latest wins).

---

## 6. Self-Knowledge & Memory Integration

1. **System Prompt Injection:** Prompt engine (`prompts.py`) injects a concise summary of the active look:
   > `[Appearance: Humanoid Female, wearing Wizard Robe, sneakers, glasses, palette blue/amber]`
2. **Episodic Memory Note:** Upon completing a redesign, `reflect.py` writes a long-term memory note:
   > `Memory (2026-09-25): Autonomously adopted wizard robe outfit with neon glasses following a high-energy reflection cycle.`