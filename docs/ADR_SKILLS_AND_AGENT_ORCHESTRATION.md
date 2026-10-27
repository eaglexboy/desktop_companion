# Architecture Decision Record: Extensible Skills & Sub-Agent Orchestration Framework

* **Status:** Accepted for V2+ Roadmap (Deferred from V1; V1 Interface Accommodations Approved)
* **Date:** 2026-09-30 (amended 2026-10-26: adopt the open Agent Skills standard, §4 and §9; skill activation and context budget, §4 "Skill Activation & Context Budget")
* **Deciders:** Architecture & Core Engineering Team
* **Supersedes:** None
* **Related:** [ADR_MCP_INTEGRATION.md](ADR_MCP_INTEGRATION.md)

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
7. **Adopt the Agent Skills open standard (`SKILL.md`) for third-party skills** ([agentskills.io](https://agentskills.io/specification)) instead of a bespoke manifest, so the companion can use skills written by others. Our own settings ride in the spec's `metadata` map under `deskmate.*` keys. Imported skills are untrusted, vetted on import, and **inert until the Track 3 runtime and sandbox exist** (§4, §9).
8. **Budget the context skills consume.** Skills are enabled and pinned explicitly (imports start disabled), only a bounded set of one-line stubs ever enters the companion's prompt, selection escalates from local keyword search to the routing model only when several candidates remain, and a skill's full instructions load into the agent that runs it, never the companion, which supervises by clearing plans and checking at checkpoints (§4, "Skill Activation & Context Budget"). The model can never enable a skill.
9. **Define V1 Non-Breaking Accommodations:** Reserve tool routing interfaces, concurrency configuration keys (`max_parallel_slots`, `max_concurrency`, `max_script_workers`), and task state event schemas in V1 to ensure V2 introduces zero breaking changes.

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
│   "Run git pull &&    |  weather in Tokyo?",    |   write tests, report back"   │
│    make build"        |  "Search docs for X"    │                               │
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

Deskmate has two kinds of skill, deliberately kept apart:

| | **Typed actions** (today) | **Agent Skills** (`SKILL.md`, this ADR) |
|---|---|---|
| Defined by | Python `Skill(...)` registered in `brain/skills/builtin.py` | A directory with a `SKILL.md`, per the open standard |
| Author | Us | Anyone (community repos, the user) |
| What it is | A validated function the reply format can call: `{"do":[{"skill":...}]}` | Markdown instructions (+ optional `scripts/`, `references/`, `assets/`) an agent follows with its tools |
| Runs in | The companion's single reply turn (Track 2-like) | A Track 3 agent loop, or Track 1 when the skill is a script pipeline |
| Status | Live | Parsed, vetted, catalogued; **not executed** until Phase 2 |

The typed actions stay as they are. They are the companion's own body (gestures, memory, appearance) and are not worth wrapping in markdown.

### 1. Skill Format: Agent Skills (`SKILL.md`)

Skills live in `<deploy_dir>/skills/<source>/<name>/` (override with `[skills] dir`); `<source>` is where the skill came from (`local`, or a label derived from its repository). The package layout and frontmatter are exactly the standard's:

```
pdf-tools/
├── SKILL.md        # required: YAML frontmatter + markdown instructions
├── scripts/        # optional executable code (not installed by default)
├── references/     # optional docs, loaded on demand
├── assets/         # optional templates and data
├── LICENSE         # the upstream license, always preserved
└── .source         # ours: provenance written by the importer
```

```markdown
---
name: pdf-tools                      # required; 1-64 chars, a-z 0-9 and single hyphens; matches the directory
description: Extract text from PDFs. Use when the user mentions PDFs.   # required; 1-1024 chars
license: Apache-2.0                  # optional
compatibility: Requires poppler-utils    # optional, <= 500 chars
allowed-tools: Bash(pdftotext:*) Read    # optional, experimental in the spec; a request, never a grant
metadata:                            # optional free-form map; our settings live under deskmate.*
  deskmate.track: agent              # script | tool | agent   (default: agent)
  deskmate.allow_network: "false"    # string "true" only; anything else is false
---
# Steps
1. ...
```

**Progressive disclosure** (from the standard, and it matters for small local models): at startup only `name` + `description` (about 100 tokens each) enter the catalog; the body is loaded when a skill is activated; `references/` and `assets/` are read on demand. A catalog of hundreds of skills must never be pasted into the companion's prompt.

**Our extensions** are only ever read from `metadata` keys prefixed `deskmate.`, so a skill remains valid for every other implementation. Absent keys mean the most restrictive reading, which is why an unmodified community skill is always safe to *load*:

| Key | Meaning | Default |
|---|---|---|
| `deskmate.track` | `script`, `tool` or `agent` (§2) | `agent` |
| `deskmate.requires_desktop_presence` | needs the on-screen companion | `false` |
| `deskmate.allow_network`, `allow_terminal_exec` | sandbox grants a script skill asks for | `false` |
| `deskmate.allow_filesystem_read`, `allow_filesystem_write` | comma-separated path roots | none |

These replace the former `skill.toml` `[permissions]` table; the §2 enforcement and §3 approval flow are unchanged. A skill's own request (`allowed-tools`, `deskmate.*`) is only ever an *ask*: the user's pre-approval decision stays the only grant.

### 1a. Import Flow, Trust Tiers & Vetting

`python -m brain.skills.importer <https-git-url | dir> [--skill NAME]` (implemented in `brain/skills/importer.py`):

1. **Fetch** shallowly over https only, with symlinks checked out as plain files, a sanitized environment and no prompts.
2. **Discover** every `SKILL.md` in the repository; **validate** each strictly against the standard. Anything invalid is reported and skipped, and one bad skill never blocks the rest. Tolerated deviations (directory name differs from `name`, `allowed-tools` written as a list) become warnings.
3. **Refuse** packages containing symlinks, larger than 20 MiB, or whose instructions match a prompt-hijack pattern (`prompt_override`, `identity_override`, `jailbreak`, `delimiter_injection`) unless the user passes `--accept-findings`.
4. **Label**, not refuse, command-shaped text (`curl ... | sh`, `POST http://`), `allowed-tools` naming `Bash`/`Write`/`Edit`, and any `scripts/` directory. Reason: measured on 231 real community skills these are ordinary in tool documentation, and a regex cannot tell documentation from an attack. They set the trust tier to `flagged` and are enforced at *run* time by the sandbox and permission flow.
5. **Install** to `<source>/<name>/` without `scripts/` unless `--with-scripts`; never overwrite without `--force`; copy the upstream `LICENSE`; write `.source` (origin, commit, license, tier, findings).

| Tier | Meaning | Run policy (Phase 2) |
|---|---|---|
| `bundled` | ships with Deskmate | normal |
| `local` | the user wrote or placed it | normal |
| `imported` | fetched from a source; scanned clean | instructions only; no scripts |
| `flagged` | imported with findings, scripts or dangerous tools | every dangerous call asks first; never `always_allow` without an explicit user act |

Imported skills are **inert in V1/Phase 1**. They appear on the status page (`/api/skills` -> `installed`, `rejected`) so the user can see what is installed and why anything was refused, but nothing executes them. Skills are third-party instructions fed to a model: the sandbox in §2 and the approval flow in §3 are prerequisites for running them, not enhancements.

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

### 4. Skill Activation & Context Budget

**Problem.** A catalog can be far larger than any prompt. Measured on 227 published community skills (the Hermes Agent and Anthropic repositories; tokens estimated as characters ÷ 4):

| Content | Tokens |
|---|---|
| Name + description of all 227 (the standard's "metadata" tier) | about 6,000 |
| Median / largest single description | about 18 / about 250 |
| Full instructions (`SKILL.md` bodies) of all 227 | about 580,000 |
| A 13,700-skill catalog (OpenClaw scale; extrapolated) | about 360,000 for names and descriptions alone |

Even the metadata tier alone does not fit a small local model on every turn, so "progressive disclosure" of bodies is necessary but not sufficient. The cost of skills in the prompt must be **independent of how many are installed**.

#### 4.1 Invariants

1. **O(budget), not O(N).** The tokens spent on skills in any prompt never exceed a budget derived from the active backend's context size (4.4), however many skills are installed. Enforced where the prompt is assembled, and covered by a test with a catalog far larger than the budget.
2. **Disabled means absent.** A disabled skill costs zero tokens and cannot be reached by any call, including a model that guesses its name (the registry already behaves this way for typed actions).
3. **The model cannot enable skills.** Enabling a skill widens what the companion may do, so only the user can do it, with an explicit confirmation (4.2). A model reply, a tool result or a skill's own text can never enable anything. *Disabling* is safe and needs no confirmation.
4. **Skill search never costs an LLM call on ordinary dialogue.** The keyword search is local and runs only when a request looks like it needs a skill. A model call happens only to choose between several remaining candidates (4.3), uses the small routing model first, and never delays a plain conversational turn.
5. **Over budget degrades visibly.** Lowest-ranked entries are dropped and a `skill.context` event records what was dropped. Text is never silently truncated mid-instruction.

#### 4.2 State: installed, enabled, pinned

Three independent facts per skill, not one switch:

| Fact | Meaning | Who sets it | Default |
|---|---|---|---|
| **installed** | on disk under `<skills dir>/<source>/<name>/` | the importer | |
| **enabled** | eligible to be offered at all | the user (invariant 3) | `local`/`bundled`: yes. **`imported`: no.** `flagged`: no, and the UI says why |
| **pinned** | always shown to the companion as a one-line stub | the user | no |

Exposure levels, each strictly more expensive than the last:

| Level | What is in context | When |
|---|---|---|
| L0 absent | nothing | not enabled |
| L1 discoverable | nothing per skill; one fixed hint line naming `find_skill` and the categories of enabled skills (4.3) | enabled, not pinned |
| L2 stub | `name: first sentence of description`, capped at 160 chars | pinned, or returned by this turn's search |
| L3 activated | the full `SKILL.md` body | loaded into the agent that runs the task |
| L4 resource | one `references/` or `assets/` file | the activated skill asks for it |

State is stored in SQLite (§6, `skill_state`), not in `config.toml`, so it can change at runtime without a restart.

**Confirming an enable.** Enabling needs a confirmation the user can give **without a microphone**: a typed answer or a click. **A spoken "yes" is never accepted**, for any skill: it can be produced by anyone in earshot or by the companion's own speaker. A spoken request may *start* an enable ("turn on the arxiv skill"); the companion then asks for the typed or clicked confirmation. A `flagged` skill requires the stronger form, the skill's name typed out. A confirmation not given within 60 seconds leaves the skill disabled.

Where that confirmation happens: the **CLI** (`python -m brain.skills.manage enable <id>`, which asks you to type the answer), the brain's **`POST /api/skills/state`** (the call refuses unless `confirmed` is true, plus `confirm_text` equal to the skill name for `flagged` ones; the client is responsible for really asking), and later the **overlay** (typed in the input bar or a click on the prompt). The **status page stays read-only**, as its own spec requires: it shows each skill's state and the token meter but has no switches. The model cannot reach any of these: no typed action can change skill state, and a test asserts it.

Precondition for Phase 2: the brain's HTTP API has no authentication today (`[server] auth_token_file` is reserved). Because enabling is inert in V1 this is acceptable now; **before any imported skill can run, state-changing endpoints must be authenticated**, or any local process could enable a skill.

**Identity and name clashes.** The canonical identifier is always `<source>/<name>` (for example `hermes-agent/arxiv`). It is what `skill_state`, pins, logs and task records store, and it never changes. People and the model use a short form:

* The bare `name` works only while it is unique among installed skills.
* When two installed skills share a name, the bare name becomes ambiguous (the companion asks) and the **static suffix `@<source>`** is always valid: `pdf@hermes-agent`, `pdf@anthropics-skills`.
* The suffix is the source label chosen at install time, so it is fixed by the install, not by what else is installed. The user may set a short `alias` per skill (`skill_state.alias`).
* *Why static, not a dynamic counter (`pdf`, `pdf-2`):* a counter depends on install order and would silently change what an old reference means. A suffix drawn from the source never shifts under an existing reference. The default label should be the repository name alone (`hermes-agent`), falling back to `owner-repo` only when two sources would collide.

#### 4.3 Selection without listing

```
user request ──► keyword search over enabled skills (name + description + tags + category)
                        │  local, in-memory index, no model call
                        ├─ 0 candidates ──────────────► answer without a skill
                        ├─ 1 candidate ───────────────► select it
                        ├─ 2-3 candidates, and the top score beats the runner-up by winner_margin
                        │                              ► select the top one
                        └─ 2-3 close candidates, or more than 3 candidates
                                                       ► routing model chooses among their stubs
                                                         (main model only if the routing model is
                                                          unavailable or not confident)
                        ▼
   model proposes use_skill(<id>); the registry validates it (enabled, exists)
                        ▼
   Supervisor starts a Track 3 task whose context holds ONLY that skill (L3)
```

* **Least context first.** The cheapest mechanism that works is used, and each step escalates only if the previous one leaves more than one option. The rule is deterministic and unit-tested without any model: **one candidate wins outright; two or three candidates are decided by score if the leader's score is at least `winner_margin` times the runner-up's; more than three candidates always go to the routing model**, which sees at most `search_top_k` stubs. `winner_margin` starts at 1.5 and is tuned on the measured corpus.
* **Send enough, let the model ask for more.** Context is assembled for the smallest eligible backend window, and what is sent is a small, ranked, bounded set. A model that needs more calls `find_skill` again with a different query or category, or asks for a reference file (L4). Pulling is cheap; over-sending is not.
* **Categories first, skills second.** Many catalogs (Hermes among them) group skills by category. The importer records each skill's `category` (its parent path inside the source) in `.source`; categories are indexed as tags and the L1 hint lists the categories of enabled skills (at most `max_hint_categories`, inside the budget), so the model can narrow before it searches: `find_skill(query, category?)`.
* **Index:** a small in-memory BM25 index over the metadata tier, rebuilt on install, enable or disable. Pure Python, no new dependency. Embeddings are a later option, justified only by measurement.
* **Quality depends on descriptions.** The standard asks authors to say what a skill does *and when to use it*. The user may add search tags in `metadata` (`deskmate.tags`).
* **Typed actions are counted too.** The built-in actions' prompt block is accounted against the same budget; it is small, but not exempt. Built-ins keep their own `[skills] disabled` list (4.7): turning one off is a different decision from turning off a third-party skill.

#### 4.4 Context size and budget configuration

Budgets must scale with the backend actually answering. Context size is a property of the backend: fixed for local inference (the hardware is the limit), different for each cloud model. It is recorded in config, not guessed, and **the same field exists on every backend, local or cloud, so the budget code is identical wherever the model runs**:

```toml
[llm.local]
context_tokens = 8192            # set to the real value of the loaded model (e.g. llama-server -c)

[llm.cloud.anthropic]            # per provider; a per-model table is allowed where one provider serves several
context_tokens = 200000

[skills]
catalog_fraction = 0.03          # share of the backend's context that stubs may use
catalog_token_cap = 400          # ...never more than this, however large the window
activation_fraction = 0.25       # share a worker may spend on the activated skill body
winner_margin = 1.5              # leader must beat the runner-up by this factor to skip the routing model
max_auto_candidates = 3          # more candidates than this always go to the routing model
max_pinned = 8
max_active_skills = 1            # skills loaded into one worker at a time
search_top_k = 5
max_hint_categories = 12
```

The budget is `min(cap, fraction × context_tokens)`. Because the router can fail over between local and cloud, the prompt is assembled for the **smallest eligible window** and the model pulls more when it needs it (see "Send enough", 4.3). The same `context_tokens` value is what a future **session-compaction** feature needs to know when to summarise history; compaction is a separate decision, out of scope here.

#### 4.5 The companion as supervisor

When a task uses a skill, the **agent** owns the skill: it loads the body, decides how to apply it, and its context holds the instructions (4.3). The companion does not execute the skill and does not micromanage steps. It is the supervisor, with three jobs:

1. **Clear the plan before execution.** The agent first states what it is about to do (for Track 3, the `propose_work_plan` contract in §5, or a short step list for a single task). The companion checks it before anything runs. Deterministic checks come first: the skills and tools named are enabled and in the task's allowed set; the plan is within the turn, token and time budgets; nothing in it needs a permission that has not been granted. Only if those pass, and only when the plan is large or touches something sensitive, does a model read it to ask "does this match what the user asked for?" It may approve, ask the agent to revise, or ask the user.
2. **Track what is done and what is pending.** Task and step state is recorded (`plans`, `tasks`, `task_logs`, §6). The companion can answer "how is that going?" from this record without asking the agent, and shows progress through `agent_task_progress` events.
3. **Intervene at defined checkpoints only.** Between plan approval and completion the companion does not review each step. The checkpoints are the ones that already exist: a dangerous call raising a permission request (the Permission Pre-Approval Flow above), a budget being exhausted, a step failing, and the user cancelling. At each, the companion decides whether to continue, pause, ask the user or stop.

The principle is the same as in 4.3: deterministic checks first, a small model next, the main model last.

**What counts as a "big" or "sensitive" plan (proposed; to be tuned).** Both are task-relative, so neither is a fixed number. The approach:

1. **A deterministic floor.** Every tool belongs to a risk class (`read`, `write`, `exec`, `network`, `credentials`, `irreversible`; the sandbox grants already name most of them). A plan's oversight level is computed from what it contains: any `irreversible` or `credentials` step, or `exec`/`write` outside the task's own working area, sets `high`; any `write`, `exec` or `network` sets `normal`; read-only sets `low`. Size is judged against the task's own budgets and history (a plan notably larger than its declared `max_turns`, or than the median for that skill, is bumped one level), not against a global constant.
2. **The model may only raise it.** After the floor is computed, the supervisor (routing model first) may escalate the level based on context ("this touches the user's tax folder"). It can never lower it below the floor, so a mistaken or manipulated model call cannot make a risky plan quieter.
3. **The level tells the agent how often to report.** It becomes part of the task envelope: `oversight = {level, report_every_steps, approve_before: [risk classes]}`. `low`: report at completion. `normal`: report at each milestone. `high`: approval before each step in the listed risk classes, plus a periodic heartbeat. The agent reports; the companion does not poll.
4. **Escalation during the run.** A failed step, a budget at 80%, or an unexpected tool request raises the level for the rest of the task.

#### 4.6 Observability

* Per turn, a `skill.context` event: `{pinned, offered, activated, tokens_used, budget, dropped}`.
* The status page (read-only) shows each skill's state (installed, enabled, pinned, tier, exposure level), a meter of skill tokens against budget, and the dropped list. This is how you notice trouble before the model degrades. Changes are made with the CLI or the brain API (4.2).
* `GET /api/skills` carries `id`, `enabled`, `pinned`, `exposure` and `stub_tokens` per skill, and a `budget` summary.

#### 4.7 Phasing, decisions and what is still open

| Phase | Scope |
|---|---|
| **V1 (now)** | `context_tokens` on every backend; `skill_state` table; enable/pin through the CLI and brain API with typed confirmation (status page read-only); stub builder and token estimator; importer records `category` and uses repository-name source labels; the budget meter. Imported skills stay inert and are not put in the prompt, so the meter reports what *would* be sent. Invariant 1-3 tests. |
| **Phase 2** | Keyword index, `find_skill` / `use_skill`, routing-model disambiguation; worker-scoped activation (L3/L4); plan clearing and checkpoints (4.5); voice-started enables. |
| **Later, if measured necessary** | Embeddings; usage-based ranking; session compaction (its own ADR). |

**Decided (2026-10-26):**

| # | Decision |
|---|---|
| 1 | Imports start **disabled**; the user enables each one. |
| 2 | Context size is a **config value on every backend**, local and cloud alike (`context_tokens`); budgets are fractions of it with absolute caps. |
| 3 | The **agent owns the skill** and loads it; the companion supervises: it clears the plan, tracks progress and intervenes at checkpoints, not per step (4.5). |
| 4 | **Least context first:** keyword search; one candidate wins; two or three go by score margin; more than three go to the routing model, then the main model only if needed. |
| 5 | Built-in typed actions keep their **separate** `[skills] disabled` list. |
| 6 | Enabling is confirmed by a **typed or clicked answer**; a spoken "yes" is **never** accepted; flagged skills need the name typed (4.2). |
| 7 | Name clashes use a **static `@<source>` suffix** on a never-changing `<source>/<name>` id; the user may set an alias (4.2). |
| 8 | **Categories** are recorded and used to narrow search (4.3). |
| 9 | `max_pinned = 8` and 160-character stubs are starting values, to be tuned. |
| 10 | **Send enough, let the model ask for more:** assemble for the smallest eligible window; the model pulls more by calling `find_skill` or asking for a reference. |
| 11 | The status page stays **read-only**; skill switches live in the CLI and brain API (and later the overlay). |

**Still open (to settle while building):**

1. **Plan oversight levels (4.5):** the risk classes, the thresholds that bump a level, and the default reporting cadence per level are proposals and need to be tried on real tasks.
2. **`winner_margin` value and the routing model's prompt** need a query set against the 227-skill corpus; record recall@5 before and after.
3. **Per-model context sizes** for a cloud provider that serves several models: one `context_tokens` per provider entry is enough while an entry names one model, which is the current config shape.
4. **API authentication** (4.2) before Phase 2.

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

CREATE TABLE IF NOT EXISTS skill_state (
    skill_id TEXT PRIMARY KEY,           -- "<source>/<name>", e.g. "hermes-agent/arxiv"
    enabled INTEGER NOT NULL DEFAULT 0,  -- 1 = eligible to be offered
    pinned INTEGER NOT NULL DEFAULT 0,   -- 1 = always shown as a stub (requires enabled)
    alias TEXT,                          -- optional user-chosen short name
    changed_by TEXT NOT NULL,            -- "default", "cli", "api", "overlay"
    updated_at REAL NOT NULL
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
   Reserve `max_parallel_slots = 1` under `[llm.local]`, `max_concurrency = 4` under `[llm.cloud.<provider>]`, and `max_script_workers = 4` under `[skills]` in `config.example.toml` and [SPEC_BRAIN.md](SPEC_BRAIN.md).
4. **Task State & Permission Event Skeletons:**
   Reserve `agent_task_progress` (including `plan_id`), `agent_task_complete` (including `plan_id`), `agent_permission_request`, and `agent_permission_response` in the WebSocket IPC protocol taxonomy as recognized but unhandled V1 event types.

---

## 8. Actionable Exit Criteria & Automated Testing Matrix

* **V1 Readiness:**
  * Agent Skills are parsed and validated (`brain/skills/agentskills.py`), vetted and installable (`importer.py`, `safety.py`), listed on the status page, and never executed.
  * `context_tokens` is configurable per backend; skill enable/pin state persists in `skill_state`; imports start disabled and enabling needs a typed or clicked confirmation; the CLI and brain API change it and the (read-only) status page shows state and tokens spent on skills against the budget.
  * With a catalog 100x larger than the budget, the skills portion of the prompt stays within `catalog_token_budget` (test).
  * A disabled skill cannot be reached by any call; nothing in a model reply or tool result can enable one (tests).
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

* **Phase 2 additions (skill selection):** keyword index, `find_skill`/`use_skill`, routing-model disambiguation and authenticated state-changing endpoints; the body loads only into the agent that runs the task; the companion clears plans before execution; recall@5 on the 227-skill community corpus measured and recorded before any embedding work.

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

---

## 9. Amendment 2026-10-26: Open Standard & Prior Art

**Why the change.** The original §4 specified a bespoke `skill.toml`. Skills are now an open standard with a large community catalog (Anthropic's reference skills, Hermes Agent, and others). A private format would cut the companion off from all of it for no gain, since the standard's `metadata` map carries everything we needed.

**What running the importer on real skills found:** run against the Hermes Agent and Anthropic skill repositories, the importer found 231 skills and installed 227 (141 `imported`, 86 `flagged`). Four were refused: two as suspected prompt injection, one with an underscore in its name, one with a description over 1024 characters. The scan as inherited from [OpenJarvis](https://openjarvis.stanford.edu/) refused 38 of the 231, almost all false positives on tool documentation, so its command-shaped patterns were downgraded (see `brain/skills/safety.py`).

**What is not decided here.** Most community skills assume an agent with shell, file and web tools and use Claude Code's tool names (`Bash`, `Read`, `Write`...). Running them needs the Phase 2 runtime, a tool-name translation layer onto our own tools, and the sandbox. The OpenJarvis `ToolTranslator` table is the reference starting point for that mapping. MCP is the other source of tools: [ADR_MCP_INTEGRATION.md](ADR_MCP_INTEGRATION.md).

**Attribution.** Parts of the importer, scanner and parser are adapted from [OpenJarvis](https://openjarvis.stanford.edu/) (Apache-2.0, Copyright 2025 The OpenJarvis Authors); see [`NOTICE`](../NOTICE) and the header of each file.
