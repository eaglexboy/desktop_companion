# Architecture Decision Record: Extensible Skills & Sub-Agent Orchestration Framework

* **Status:** Accepted for V2+ Roadmap (Deferred from V1; V1 Interface Accommodations Approved)
* **Date:** 2026-09-30
* **Deciders:** Architecture & Core Engineering Team
* **Supersedes:** None

---

## 1. Context & Executive Recommendation

As a desktop companion, Deskmate requires capabilities beyond conversational dialogue. Users expect the companion to:

1. **Execute Skills:** Perform specialized actions on the local desktop (e.g., window control, media control, file operations, web research, script automation).
2. **Spawn Autonomous Sub-Agents:** Delegate long-running or multi-step goals (e.g., *"Research this topic and summarize it,"* *"Watch this build and notify me when it fails,"* *"Audit this repository, fix formatting, and run the test suite"*) to focused worker agents while the main companion remains responsive to user interaction.

### The Core Architectural Dilemma: Heterogeneous Compute & Starvation Risk

* **Compute Heterogeneity:** Users run Deskmate across vastly different hardware—from laptops running a single quantized 7B model locally on Ollama or vLLM, to multi-GPU workstations with concurrent inference slots (e.g., vLLM with continuous batching on a dedicated AI rig), to cloud-only setups using commercial APIs (Claude, OpenAI) governed by strict Requests-Per-Minute (RPM) and Tokens-Per-Minute (TPM) rate limits.
* **The Starvation Invariant:** The companion's primary interactive loop (voice STT/TTS and real-time dialogue) must **never freeze, lag, or become unresponsive** because a background sub-agent is saturating local VRAM or exhausting cloud API quotas.
* **Task Complexity Spectrum:** Tasks range from **zero-LLM deterministic pipelines** (e.g., *"Run script A, then script B, then parse stdout"*) to **heavy multi-turn cognitive loops** requiring planning, reflection, and tool execution.

### The Decision:

1. **Defer Full Agent/Skill Execution to V2+:** V1 remains dedicated to the core on-screen companion experience, presence evasion, and low-latency voice dialogue.
2. **Adopt the 3-Track Skill Model:** Partition abilities into **Deterministic Script Pipelines** (Track 1: `"script"`), **Single-Turn Tool Augmentations** (Track 2: `"tool"`), and **Autonomous Cognitive Sub-Agents** (Track 3: `"agent"`).
3. **Implement the Inference Capacity Governor (ICG):** A dynamic scheduler that manages LLM concurrency slots, reserves VIP Priority 0 for the interactive companion, and automatically throttles or queues background sub-agents based on runtime backend capabilities.
4. **Hierarchical Work Plan (DAG) Orchestration:** Sub-agents cannot recursively spawn further agents directly. If a complex task requires multiple workers, the agent proposes a structured **Work Plan (Task DAG)** back to the Companion Supervisor. The Companion coordinates, resolves dependencies, and schedules worker agents according to ICG capacity.
5. **Dedicated Task Fallback for Single-Slot Setups:** When only a single local inference slot exists with no cloud fallback, the companion offers conversational delegation into a focused, non-concurrent mode rather than silently failing.
6. **Sandboxing & Permission Pre-Approval:** Enforce OS-level containerization for terminal/disk scripts via Linux Bubblewrap (`bwrap`) with persistent user pre-approval policies (`"allow_once"`, `"always_allow"`, `"deny"`). Fail fast if `bwrap` is missing; enterprise container/root fallbacks (`chroot`, Docker) are rejected as anti-patterns for an unprivileged desktop daemon.
7. **Define V1 Non-Breaking Accommodations:** Reserve tool routing interfaces, concurrency configuration keys (`max_parallel_slots`, `max_concurrency`, `max_script_workers`), and task state event schemas in V1 to ensure V2 introduces zero breaking changes.

---

## 2. Task Complexity Spectrum & Execution Tracks

Skills and delegated goals are categorized into three distinct execution tracks based on their compute and concurrency demands:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           Task Complexity Spectrum                              │
├───────────────────────┬─────────────────────────┬───────────────────────────────┤
│ Track 1: Script       │ Track 2: Tool           │ Track 3: Agent                │
│ (track = "script")    │ (track = "tool")        │ (track = "agent")             │
├───────────────────────┼─────────────────────────┼───────────────────────────────┤
│ • Zero LLM inference  │ • 1 LLM turn (Tool Call)│ • Multi-turn reasoning loops  │
│ • Pure Python / Bash  │ • Synchronous execution │ • Independent goal reflection │
│ • Local CPU process   │ • Injected into dialogue│ • Background worker task      │
│ • Bounded process pool│ • VIP Companion turn    │ • Governed by ICG slots       │
│ • E.g.: "Mute audio", │ • E.g.: "What's the     │ • E.g.: "Audit repo, fix bugs,│
│   "Run git pull &&      weather in Tokyo?",       write tests, report back"   │
│    make build"          "Search docs for X"     │                               │
└───────────────────────┴─────────────────────────┴───────────────────────────────┘
```

### Track 1: Deterministic Script Pipelines (`track = "script"`)

* **Mechanics:** Executed via an asynchronous OS subprocess worker pool (`asyncio.create_subprocess_exec`) sandboxed via Bubblewrap (`bwrap`).
* **Resource Profile:** Consumes negligible CPU/RAM; consumes **zero LLM inference slots**.
* **Scheduling:** Bounded by a standard process pool concurrency limit configured via `[skills] max_script_workers = 4`. Does not interact with or block LLM rate limiters.

### Track 2: Single-Turn Conversational Tool Calls (`track = "tool"`)

* **Mechanics:** Standard Function Calling / Tool Calling exposed directly to `router.py`.
* **Resource Profile:** Executes synchronously within the user's ongoing conversation turn.
* **Scheduling:** Handled directly within the primary companion's VIP inference turn (Priority 0). Does not interact with ICG slot allocation.

### Track 3: Autonomous Multi-Turn Sub-Agents (`track = "agent"`)

* **Mechanics:** An isolated worker context spawned with a specific goal prompt, a scoped set of allowed tools, a step budget, and a token budget.
* **Resource Profile:** Makes repetitive, asynchronous calls to the LLM pool over minutes or hours.
* **Scheduling:** Must be strictly scheduled, throttled, or paused by the **Inference Capacity Governor (ICG)**.

---

## 3. The Inference Capacity Governor (ICG)

The Inference Capacity Governor manages inference concurrency, guaranteeing that background tasks never degrade foreground conversational latency.

```
                                  ┌────────────────────────┐
                                  │   Inference Capacity   │
                                  │     Governor (ICG)     │
                                  └───────────┬────────────┘
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      ▼                                               ▼
      ┌───────────────────────────────┐               ┌───────────────────────────────┐
      │   Local Inference Tier        │               │   Cloud Provider Tier         │
      │   (vLLM / Ollama / llama.cpp) │               │   (Anthropic / OpenAI / etc.) │
      └───────────────┬───────────────┘               └───────────────┬───────────────┘
                      │                                               │
             Configured Slot Pool                           Rate Limit Semaphores
             • Capacity: N slots                            • Token Bucket (TPM / RPM)
             • Slot 0: COMPANION VIP                        • Provider Priority Queue
             • Slots 1..N-1: Sub-Agents                     • Concurrency Cap per Provider
                      │                                               │
                      └───────────────────────┬───────────────────────┘
                                              │
                                              ▼
                              ┌────────────────────────────────┐
                              │ Priority Request Dispatcher    │
                              ├────────────────────────────────┤
                              │ Priority 0: Companion VIP      │ (Preempts immediately*)
                              │ Priority 1: User-Requested Task│ (Next available worker slot)
                              │ Priority 2: Background Agent   │ (Throttled during dialogue)
                              └────────────────────────────────┘
```

*\*Note: Priority 0 preempts immediately under all standard operating conditions. The sole, user-consented carve-out is Dedicated Task Mode (§3.3), where the user explicitly agrees to reassign Slot 0 temporarily to a background task.*

### 1. Determining Concurrency Limits & Scheduling Pseudocode

The daemon configures available inference concurrency slots authoritatively via configuration, avoiding runtime load-probing that could transiently starve the model:

#### A. Local Backend Architecture (`[llm.local]`)

Local inference engines have fixed hardware limits (GPU VRAM, compute cores, KV-cache allocations):

* **Configuration Key:** The setting `[llm.local] max_parallel_slots = N` (default `1`) is **authoritative and required**.
  * On single-stream backends (e.g. lightweight desktop Ollama or llama-server), `max_parallel_slots = 1`.
  * On continuous-batching servers (e.g., `vLLM` running on multi-GPU setups such as a dedicated AI server), `max_parallel_slots` is configured to match the server's sustained parallel sequence capacity (e.g. `4` or `8`).
* **The "Zero-Starvation" Invariant (VIP Reservation):**
  $$\text{SubAgentSlots}_{\text{local}} = \max\left(0, \text{MaxParallelSlots}_{\text{local}} - 1\right)$$
  * **Slot 0 is permanently reserved for the Companion.**
  * If $\text{MaxParallelSlots}_{\text{local}} = 1$, **no sub-agent is permitted to run in parallel in the background on the local LLM.**

#### B. Cloud Backend Discovery & Rate Limiting (`[llm.cloud.<provider>]`)

* Configured via `[llm.cloud.<provider>] max_concurrency` (default `4`).
* The ICG maintains an asynchronous token-bucket rate limiter tracking estimated tokens per minute (TPM) and requests per minute (RPM).
* If a provider returns HTTP 429 (Rate Limit), the ICG immediately backs off and pauses sub-agent execution while allowing high-priority companion turns to failover to secondary providers.

#### C. Concrete Scheduler Loop Pseudocode

```python
import asyncio
from typing import Optional

class InferenceCapacityGovernor:
    def __init__(self, max_parallel_slots: int):
        self.max_slots = max_parallel_slots
        # task_id -> slot_index (0: Companion VIP, 1..N-1: Sub-Agents)
        self.active_slots: dict[str, int] = {}
        self.dialogue_active: bool = False
        self._capacity_event = asyncio.Event()
        self._capacity_event.set()

    def set_dialogue_state(self, active: bool) -> None:
        """Triggered on interaction_state changes or status_update == 'speaking'."""
        self.dialogue_active = active
        if not active:
            self._capacity_event.set()

    async def acquire_slot(self, priority: int, task_id: str, timeout_s: float = 60.0) -> int:
        """
        Acquires an inference slot based on priority:
          Priority 0: Companion VIP (slot 0, non-blocking)
          Priority 1: User-Requested Worker Task (slot 1..N-1, waits for worker free)
          Priority 2: Background Sub-Agent (slot 1..N-1, waits for idle dialogue + worker free)
        """
        if priority == 0:
            # Companion VIP permanently claims Slot 0
            self.active_slots[task_id] = 0
            return 0

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s

        while True:
            # Priority 2 waits until dialogue is completely idle
            # All sub-agents require at least one worker slot (1 .. max_slots - 1) to be free
            max_worker_slots = max(0, self.max_slots - 1)
            active_worker_count = len([s for s in self.active_slots.values() if s > 0])

            throttle_condition = (priority == 2 and self.dialogue_active) or (active_worker_count >= max_worker_slots)

            if not throttle_condition:
                # Find lowest free worker slot in range [1, max_slots - 1]
                taken_slots = set(self.active_slots.values())
                for candidate in range(1, self.max_slots):
                    if candidate not in taken_slots:
                        self.active_slots[task_id] = candidate
                        # Correctly count only worker slots when clearing capacity event
                        active_workers = len([s for s in self.active_slots.values() if s > 0])
                        if active_workers >= max_worker_slots:
                            self._capacity_event.clear()
                        return candidate

            # Check timeout budget
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"Task {task_id} timed out waiting for capacity slot.")

            self._capacity_event.clear()
            try:
                await asyncio.wait_for(self._capacity_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError(f"Task {task_id} timed out waiting for capacity slot.")

    def release_slot(self, task_id: str) -> None:
        """Frees the acquired slot and signals waiting workers."""
        if task_id in self.active_slots:
            del self.active_slots[task_id]
            self._capacity_event.set()
```

### 2. Active Dialogue Throttling

Background Priority 2 agent calls are dynamically throttled or queued when the user is actively interacting. "Active Dialogue" is explicitly tied to existing signals:

* **Active Input:** The client reports `interaction_state: "active"` (text input bar open or microphone recording).
* **Active Audio Output:** The daemon is in `status_update: "speaking"` (TTS audio is playing back).

When either condition is true, background sub-agent requests wait in the ICG queue until both return to `idle`.

### 3. The Single-Slot Fallback: "Dedicated Task Mode"

When a user requests a Track 3 agentic task on a hardware setup where `max_parallel_slots = 1` and no cloud tier is configured:

1. **Immediate Conversational Proposal (Fail-Fast with Escalation):**
   The companion does not silently hang or queue forever. It immediately informs the user:
   > *"I don't have enough spare compute to run that in the background right now without freezing our conversation. Do you want me to dedicate myself to this task, and I'll come back and let you know when it's done?"*
2. **User Acceptance & Dedicated Execution (Preemption Carve-Out):**
   If the user agrees (*"Yes, go ahead"*):
   * This represents an explicit, user-consented carve-out to the standard Priority 0 preemption rule.
   * The companion temporarily allocates **Slot 0** to the agentic task.
   * The companion transitions to **`status_update: "thinking"`** (or specialized `task_busy` animation), appearing focused on its work.
3. **Canned Ambient Guard during Dedicated Execution:**
   * If the user attempts to interact (typing or tapping the avatar) while the companion is in dedicated mode, the companion does **not** invoke LLM inference or preempt the active task.
   * The overlay plays a canned non-verbal gesture (e.g., briefly glancing up with a *"Give me just a second, still finishing this up..."* quick audio cue or speech bubble).
4. **Immediate Task Cancellation:**
   * The user can cancel the dedicated task at any time (via `Escape`, pressing a visual cancel button on the interaction bar, or pressing global shortcut `toggle_interaction`).
   * The task is immediately cancelled, any pending LLM or child processes are terminated, and Slot 0 is restored to interactive conversation.

---

## 4. Skill Architecture & Extensibility Standard

Skills are encapsulated modules that expose tools to the router and sub-agents. Deskmate adopts a lightweight, Python-native standard compatible with the **Model Context Protocol (MCP)**.

### 1. Skill Manifest Schema (`skill.toml`)

Each skill lives in its own directory within `<deploy_dir>/skills/<skill_name>/`:

```toml
[skill]
id = "desktop_media"
name = "Desktop Media Controller"
version = "1.0.0"
author = "Core"
description = "Controls media playback (play, pause, next, volume) via MPRIS D-Bus."
track = "script"                         # "script" (Track 1), "tool" (Track 2), or "agent" (Track 3)
requires_desktop_presence = true

[permissions]
allow_dbus_services = ["org.mpris.MediaPlayer2.*"]
allow_filesystem_read = ["~/Music", "~/Videos"]
allow_filesystem_write = []
allow_network = false
allow_terminal_exec = false

[[tools]]
name = "media_pause"
description = "Pauses active desktop media players."
parameters = {}

[[tools]]
name = "media_play_pause"
description = "Toggles playback on the active media player."
parameters = {}
```

### 2. Sandboxing & Security Enforcement Mechanism

To prevent unconstrained or accidental destructive actions:

1. **OS-Level Isolation for Subprocess Scripts (Track 1):**
   * On Linux systems, skills with `track = "script"` or `allow_terminal_exec = true` execute via **Bubblewrap** (`bwrap`).
   * **Rejection of Docker and Chroot Fallbacks:** Deskmate runs strictly as an unprivileged user-level background service (`systemd --user`). `chroot` requires `root` privileges (`CAP_SYS_CHROOT`), which the daemon must never request. Running Docker introduces heavy daemon dependencies and root-equivalent socket access to run local bash commands.
   * **Prerequisite Fail-Fast:** Bubblewrap is the standard unprivileged sandbox for Linux desktops (the foundation of Flatpak). If `bwrap` is missing on a Linux host attempting to execute sandboxed skills, the runner raises an immediate diagnostic error:
     ```
     RuntimeError("Bubblewrap ('bwrap') is required for sandboxed skills. Install 'bubblewrap' via your system package manager.")
     ```
     `deploy/install.sh` includes `bubblewrap` as an explicit prerequisite.
   * Sandboxed processes receive read-only binds for system roots (`--ro-bind /usr /usr`, `--ro-bind /lib /lib`, `--ro-bind /bin /bin`), a minimal `/dev` and temporary `/tmp`.
   * Sensitive directories (`/etc`, `/boot`, `/root`, and unapproved user paths in `/home`) are completely inaccessible.
   * If `allow_network = false`, the sandbox unshares network namespaces (`--unshare-net`).

2. **In-Process Python Tool Path Validation (Track 2/3):**
   * Python-based file utilities must validate paths against declared `allow_filesystem_read` / `allow_filesystem_write` roots:
     ```python
     resolved = Path(user_path).expanduser().resolve()
     if not any(resolved.is_relative_to(Path(root).expanduser().resolve()) for root in allowed_roots):
         raise PermissionError(f"Access to path '{resolved}' is outside allowed skill boundaries.")
     ```

### 3. Permission Pre-Approval Flow & Revocation Lifecycle

Actions involving irreversible disk modification, terminal execution, or credential access require user consent:

```
Sub-Agent                  Brain Daemon (ICG)                 Overlay Client / User
    │                              │                                     │
    │── Call dangerous tool ──────►│                                     │
    │   (e.g., git clean -fd)      │── Check permission cache ───────────│
    │                              │   [If unapproved:]                  │
    │                              │── agent_permission_request ────────►│
    │                              │   (task_id, tool, preview)          │
    │                              │                                     │
    │   [Task suspended on Event]  │                                     │ [User selects:]
    │                              │                                     │ 1. Allow Once
    │                              │                                     │ 2. Always Allow
    │                              │                                     │ 3. Deny
    │                              │◄── agent_permission_response ───────│
    │                              │                                     │
    │◄── Execute or abort ─────────│── Persist "always_allow" to DB ─────│
```

1. **Permission Decision Options:**
   * **Allow Once:** Authorizes the single tool call currently pending.
   * **Always Allow:** Authorizes the tool call and registers the tuple `(skill_id, tool_name, target_pattern)` in SQLite table `skill_permissions`. Future calls matching this signature execute automatically.
   * **Deny:** Aborts the tool call immediately; returns a permission denied error string to the sub-agent.
2. **Timeout:** If the user does not respond within 60 seconds, the request times out and defaults to **Deny**.
3. **Revocation Semantics & Lifecycle:**
   * Complex time-based TTL cache expiration is unneeded enterprise overhead for a personal companion.
   * Permissions granted as `"always_allow"` persist in SQLite until explicitly revoked by the user via conversational instruction (*"Forget my permissions for git"*) or via administrative endpoint `DELETE /skills/{skill_id}/permissions`.
   * When revoked, the corresponding rows are unlinked from SQLite table `skill_permissions`, forcing subsequent dangerous tool calls to prompt the user again.

---

## 5. Sub-Agent Lifecycle & Hierarchical Orchestration

When a complex goal is assigned, Deskmate prevents unconstrained recursion while allowing multi-agent decomposition by employing a **Hierarchical Work Plan (Directed Acyclic Graph)** model:

```
User: "Audit this repository, clean up obsolete assets, and update the docs."
  │
  ▼
Companion (Executive Supervisor)
  ├─ 1. Spawns Planning Agent: goal="Create execution plan for repo cleanup"
  │     │
  │     ▼
  │   Lead Agent #1 (Planner)
  │     └─ Proposes Work Plan (DAG) via tool: propose_work_plan(plan=[...])
  │
  ├─ 2. Companion validates Plan & registers Task DAG with ICG:
  │     • Task A: "Scan repository for unused assets" (Track 3)
  │     • Task B: "Run git clean on approved assets" (Track 1, depends_on: [Task A])
  │     • Task C: "Update docs/ASSETS.md with manifest" (Track 3, depends_on: [Task A])
  │
  ├─ 3. ICG Schedules Execution:
  │     • Dispatches Task A (Worker #1)
  │     • Upon Task A completion, dispatches Task B and Task C concurrently (subject to ICG slots)
  │
  ▼
Companion: "All tasks completed! Unused assets were pruned and docs were updated."
```

### 1. The Work Plan Validation Contract (`propose_work_plan`)

Sub-agents are workers and are **programmatically barred from invoking `spawn_agent` directly**. When a lead planner decomposes a multi-step task, it invokes the built-in tool:

```json
// Tool Call: propose_work_plan
{
  "summary": "Decompose repository audit and documentation update",
  "tasks": [
    {
      "task_id": "step_1_scan",
      "goal": "Scan codebase for unreferenced assets",
      "track": "agent",
      "skills": ["file_search", "code_analysis"],
      "depends_on": [],
      "max_turns": 8,
      "max_tokens": 16000,
      "timeout_s": 120.0
    },
    {
      "task_id": "step_2_prune",
      "goal": "Prune identified asset files",
      "track": "script",
      "skills": ["git_tools"],
      "depends_on": ["step_1_scan"],
      "timeout_s": 30.0
    },
    {
      "task_id": "step_3_docs",
      "goal": "Update documentation table",
      "track": "agent",
      "skills": ["file_write"],
      "depends_on": ["step_1_scan"],
      "max_turns": 5,
      "max_tokens": 8000,
      "timeout_s": 60.0
    }
  ]
}
```

The Supervisor validates the DAG (checking for circular dependencies, unknown skill dependencies, and overall budget caps) and registers the plan in SQLite, returning an explicit response payload:

```json
// propose_work_plan Supervisor Response:
{
  "plan_id": "plan_987",
  "status": "approved",
  "total_tasks": 3,
  "execution_graph": {
    "step_1_scan": {"status": "ready", "slot_tier": "agent"},
    "step_2_prune": {"status": "blocked", "waiting_on": ["step_1_scan"]},
    "step_3_docs": {"status": "blocked", "waiting_on": ["step_1_scan"]}
  }
}
```

### 2. Safety & Lifecycle Invariants

1. **Centralized DAG Scheduling by Companion Supervisor:**
   * Only the primary Companion Supervisor and the ICG evaluate and approve work plans.
   * The ICG validates the total aggregate budget (max turns, tokens, and wall-clock timeout) and enforces DAG dependency ordering.
   * Tasks with met dependencies execute in parallel if slots allow (`SubAgentSlots > 1`), or sequence automatically on constrained hardware.
2. **Strict Budget Limits:**
   * Every individual task is assigned hard limits: `max_turns` (default 10), `max_tokens` (default 32k), and `timeout_s` (default 300s).
   * Aggregate plan limits prevent cascading cost overruns: maximum total tasks per plan = `8`.
3. **Overlay Visual Synchronization:**
   * When agents are working, the daemon emits `agent_task_progress: {task_id, plan_id, description, percent}` over WebSocket.
   * The desktop overlay client maps this to V2+ **Agentic Tool Execution Animations** without blocking normal idle movements.
4. **Comprehensive Cancellation:**
   * The user can cancel active tasks or entire plans at any time (*"Stop that scan"*, pressing `Escape` during dedicated mode, or via `POST /tasks/{task_id}/cancel`).
   * Cancellation immediately issues `task.cancel()` to the worker's `asyncio.Task` (instantly terminating pending HTTP requests to local/cloud LLMs), releases ICG concurrency reservations, and issues `SIGTERM`/`SIGKILL` to any spawned Bubblewrap child processes.

---

## 6. Persisted State Schema (`state.db`)

In V2, sub-agent tasks, DAG dependencies, logs, and pre-approved permissions are tracked in SQLite within `<deploy_dir>/state.db`:

```sql
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT NOT NULL,                -- "planning", "executing", "completed", "failed", "cancelled"
    goal_description TEXT NOT NULL,
    total_tasks INTEGER NOT NULL,
    completed_tasks INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    plan_id TEXT,                        -- Optional reference to parent DAG plan
    parent_task_id TEXT,                 -- Direct parent task if spawned via work plan
    depends_on TEXT,                     -- JSON array of task_ids that must complete before execution
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT NOT NULL,                -- "pending", "ready", "running", "paused", "completed", "failed", "cancelled"
    track TEXT NOT NULL,                 -- "script", "tool", "agent"
    goal_description TEXT NOT NULL,
    skill_manifests TEXT NOT NULL,       -- JSON array of required skill IDs
    assigned_provider TEXT,
    turns_used INTEGER DEFAULT 0,
    max_turns INTEGER DEFAULT 10,
    max_tokens INTEGER DEFAULT 32000,
    tokens_used INTEGER DEFAULT 0,
    timeout_s REAL DEFAULT 300.0,
    result_summary TEXT,
    error_message TEXT,
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts REAL NOT NULL,
    step_number INTEGER NOT NULL,
    action_name TEXT NOT NULL,
    input_payload TEXT,
    output_payload TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS skill_permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    target_pattern TEXT NOT NULL,        -- Regex or path prefix matching authorized operations
    decision TEXT NOT NULL,              -- "always_allow"
    created_at REAL NOT NULL,
    UNIQUE(skill_id, tool_name, target_pattern)
);
```

---

## 7. The Four V1 Non-Breaking Accommodations

To ensure V1 code seamlessly accommodates V2 skills, DAG work plans, and sub-agents without breaking architectural rewrites, the following lightweight hooks are incorporated into the V1 specifications:

1. **Dynamic Tool Registration Interface in `router.py`:**
   Expose an extensible tool registration hook `register_tool(name: str, description: str, parameters: dict, handler: Callable)` in `router.py`, allowing V2 skills to plug in dynamically alongside V1's hardcoded `change_appearance` and `revert_appearance`.
2. **Subprocess Execution Utility in `brain/`:**
   Standardize background subprocess execution on `asyncio.create_subprocess_exec` with environment sanitization (already implemented for `blender_gen.py`), making it reusable for Track 1 script skills.
3. **Reserved Concurrency Configuration Keys:**
   Reserve `max_parallel_slots = 1` under `[llm.local]`, `max_concurrency = 4` under `[llm.cloud.<provider>]`, and `max_script_workers = 4` under `[skills]` in `config.example.toml` and `SPEC_BRAIN.md`.
4. **Task State & Permission Event Skeletons:**
   Reserve `agent_task_progress` (including `plan_id`), `agent_task_complete` (including `plan_id`), `agent_permission_request`, and `agent_permission_response` in the WebSocket IPC protocol taxonomy as recognized but unhandled V1 event types.

---

## 8. Actionable Exit Criteria & Automated Testing Matrix

* **V1 Readiness:**
  * `router.py` exposes open tool registration.
  * Config parses reserved concurrency keys (`max_parallel_slots`, `max_concurrency`, `max_script_workers`).
  * Subprocess utilities are centralized.
  * IPC schema reserves task and permission event envelopes.
  * System verifier checks `bwrap` availability or package prerequisites.

* **Phase 1 (V2 Skills):**
  * Track 1 deterministic skills load from `<deploy_dir>/skills/`.
  * Companion executes media and system commands via tool calls inside Bubblewrap sandboxes.
  * Permission decisions persist in SQLite; `DELETE /skills/{id}/permissions` clears cache cleanly.
  * Missing `bwrap` raises descriptive diagnostic failure directing user to install it.

* **Phase 2 (V2 Agents & ICG):**
  * Inference Capacity Governor enforces slot reservations via `acquire_slot` / `release_slot`.
  * Priority 0 VIP Companion turns preempt immediately without blocking on background tasks.
  * Background Priority 2 sub-agent requests wait when `interaction_state == "active"` or `status_update == "speaking"`.
  * Dedicated Task Mode activates when slots $= 1$, triggers canned ambient gesture on user interaction attempt, and cancels cleanly on `Escape` key.
  * Task cancellation triggers both `asyncio.Task.cancel()` on pending HTTP LLM loops and `SIGTERM`/`SIGKILL` to subprocess children.

* **Phase 3 (Full Hierarchical Orchestration):**
  * Lead planner invokes `propose_work_plan`, receiving `plan_id` and registered DAG execution graph.
  * Supervisor schedules tasks based on dependency graph and ICG slot capacity.
  * WebSocket emits `agent_task_progress` with `plan_id`, and overlay visualizes progress.