# Architecture Decision Record: Multi-User on One Device

* **Status:** Proposed.
* **Date:** 2026-11-11
* **Deciders:** Architecture & Core Engineering Team
* **Related:** [ADR_NETWORK_AND_MOBILE_STRATEGY.md](ADR_NETWORK_AND_MOBILE_STRATEGY.md) (loopback binding, Origin/Host guards, auth token), [ADR_PERSONALITY_PROFILES.md](ADR_PERSONALITY_PROFILES.md) (profiles are per-companion, not per-user), [SPEC_BRAIN.md](SPEC_BRAIN.md), [SPEC_OVERLAY.md](SPEC_OVERLAY.md)

---

## 0. Terminology

Deskmate is OS and desktop-environment agnostic by contract (see README). This ADR therefore states its decisions in platform-neutral terms and maps each term to concrete mechanisms in section 2.8. Linux/KDE is the reference implementation today. Other platforms implement the same contract with their own mechanisms.

| Term | Meaning |
| --- | --- |
| **User session** | One logged-in OS account's interactive session, including a session that is logged in but switched away. |
| **Per-user config dir** | The platform's standard location for one user's configuration, readable only by that user. |
| **Per-user state dir** | The platform's standard location for one user's persistent data. |
| **Per-user runtime dir** | A private, per-user location for sockets, locks and port files that is cleaned up when the user's sessions end. |
| **Local IPC endpoint** | A local-only, OS-permissioned channel between processes of the same user (a Unix domain socket, a named pipe, or equivalent). |
| **User service** | A background process the OS starts and stops with a user session, running as that user, with no administrator rights. |
| **System service** | A background process shared by all users, started by the OS at boot or on demand. |

## 1. Context

Deskmate assumes one person per machine. A shared computer (a family PC, a lab or office workstation, a household with fast user switching) raises the question: how does each person get their own companion, with their own memories, personality, voice, API keys and avatar?

Two designs were considered:

1. **One brain per user session.** Each logged-in OS user runs their own brain, overlay and desktop sensing adapter.
2. **One brain that detects the active user and hot-swaps** the database, config and assets.

### 1.1 What is already per-session

* **The overlay and the sensing adapter are inherently per-session.** The overlay draws on one user's display. The sensing adapter lives inside that user's desktop session and reports that user's windows and monitors. A shared brain would still need one overlay and one adapter per session, so it would add routing and remove no processes.
* **State is already user-scoped by configuration.** The brain loads `config.toml` from a per-user config dir (on Linux, `$XDG_CONFIG_HOME/deskmate/`; see `brain/config.py`, `_default_config_path`). `storage.workspace_dir` and `storage.deploy_dir` are user-configured paths. They hold `state.db` (traits, mood, memories, identity), assets, logs and skills.
* **The reference deployment already uses user services.** The Linux units in `deploy/systemd/deskmate-*.service` are user units tied to the graphical session, so they start and stop with a login session.

### 1.2 What assumes a single instance

| Assumption | Where | Problem with two users |
| --- | --- | --- |
| Fixed port `8765` | `ServerConfig.port` default, `config.example.toml` | The second brain fails to bind. If the port is changed, the overlay (which defaults to `server.host`/`server.port`) must follow. |
| Host guard hard-codes `127.0.0.1:{port}` / `localhost:{port}` | `brain/ipc/server.py` (`allowed_hosts`) | Correct only if the port is read from the same settings as the bind. Currently it is, and this must stay true. |
| Loopback TCP has no caller identity | `/ws/overlay`, `/chat` | On a shared machine, any local user can connect to any user's brain and read or inject conversation. The Origin/Host guards stop browsers and DNS rebinding. They do not stop another local account. |
| Status page port `8780` | `StatusConfig.port` | Same bind collision. |
| Shared heavy resources | local LLM, TTS and STT servers | Each brain would load its own model, duplicating GPU and RAM. |

Memory in a companion is personal data (reflection stores what the user said and felt). Leaking one user's memories into another user's session is the failure the design must rule out first.

## 2. Decision

**Run one independent brain per OS user session. Do not build a user-swapping brain.**

Isolation comes from the operating system's account and file-permission model, not from application logic. The brain stays single-owner, which is the assumption the config hot-reload, reflection loop and SQLite state were built on.

### 2.1 Per-user local IPC endpoint

* The brain and overlay talk over a **local IPC endpoint** located in the per-user runtime dir (for example `<runtime-dir>/deskmate/ipc`). It is accessible to the owning user only (mode `0600` and a `0700` directory on POSIX systems; an ACL granting only the user's SID on Windows).
* This endpoint is the **default** transport when running as a user service. Another local account cannot open it, so the OS enforces peer identity.
* TCP loopback remains supported for development, tests and the remote-client roadmap in ADR_NETWORK_AND_MOBILE_STRATEGY.md. It is selected by config. When used on a host with more than one local account it requires the auth token (section 2.2) and a per-user port (section 2.3).
* The WebSocket protocol, envelopes and Origin/Host rules are unchanged. Only the transport under them changes. On the local IPC endpoint the Host check is skipped (there is no DNS to rebind) and the Origin rule is unchanged (native clients omit `Origin`).
* The presentation client and brain must both support at least TCP loopback. Support for the local IPC endpoint is the per-platform piece (section 2.8), so a platform whose client toolkit lacks it can still be multi-user using TCP with token and per-user port.

### 2.2 Authentication on TCP

* `server.auth_token_file` (currently reserved, not consumed) becomes active. At first start the brain generates a random token into a file readable only by the owning user, in the per-user config or runtime dir. The overlay reads the same file. Requests without a valid token are rejected.
* This closes the cross-user hole for any TCP binding. It is required whenever TCP is enabled and more than one local account exists. A startup check that fails closed is preferred over a warning.

### 2.3 Per-user ports (TCP only)

* No fixed port is baked into any code path that matters. A user chooses `server.port` in their own `config.toml`. `0` means "pick a free port"; the brain then writes the chosen port to a port file in the per-user runtime dir and the overlay and status page read it from there.
* `allowed_hosts` in `brain/ipc/server.py` continues to derive from the bound port, never from a constant.
* `[status].port` follows the same rule.

### 2.4 Service lifecycle

* The brain, overlay and status page run as **user services**, started and stopped with the user's session. Each user's brain runs while that user's session is alive, including a user who is logged in but switched away. A switched-away user's companion keeps running but is not visible, because the overlay draws on that user's own session.
* Running without a graphical session (a brain that outlives logout) is opt-in and platform-specific (section 2.8). It is off by default.
* A **single-instance lock** in the per-user runtime dir (an advisory file lock or a named mutex) stops a user starting two brains by accident. A second start exits with a clear message.

### 2.5 State and path hygiene

* All persistent and runtime paths resolve from the current user's platform directories (config, state/data, runtime). Defaults come from the platform, never from a shared location. The installer writes a per-user config. The brain writes nothing into machine-wide locations.
* `config.example.toml` keeps placeholder paths only. The brain rejects a `deploy_dir` or `workspace_dir` that is not owned by the current user, to catch a copied config pointing at another user's data.
* Secrets (cloud API keys, token files, voice samples) stay in the user's own `config.toml`, restricted to that user, and are never read from shared locations.

### 2.6 Shared heavy resources

* Local LLM, TTS and STT model servers (llama.cpp, Ollama, tts-serve, Whisper) are run **once, as a system service**, bound to a local IPC endpoint or a loopback port that every brain lists in its own `[llm.local]` and `[voice.*]` endpoints. Brains remain isolated from each other. They share only stateless inference.
* The shared server must not retain per-request context across clients. If an engine keeps a KV cache or session state keyed by anything shared, each brain must use distinct session identifiers or the engine must be configured stateless.
* Blender is invoked headlessly on demand. Each brain builds into its own `workspace_dir`. If concurrent builds on a small machine are a problem, the existing `max_script_workers` limit applies per brain and a machine-wide semaphore (a named lock) can be added later.

### 2.7 Desktop sensing and presentation

* The desktop sensing adapter is installed per user and talks to that user's brain only. The overlay's window belongs to that user's desktop session.
* Where a platform hosts several simultaneous interactive sessions (multiple seats, or remote desktop sessions alongside a console session), each session has its own brain. No window geometry is shared across sessions.

### 2.8 Platform mapping

The contract above is the same everywhere. These are the mechanisms each platform uses to meet it.

| Concern | Linux (reference) | macOS | Windows |
| --- | --- | --- | --- |
| Per-user config / state dir | `$XDG_CONFIG_HOME`, `$XDG_DATA_HOME` / `$XDG_STATE_HOME` | `~/Library/Application Support/Deskmate` | `%APPDATA%\Deskmate`, `%LOCALAPPDATA%\Deskmate` |
| Per-user runtime dir | `$XDG_RUNTIME_DIR` | `$TMPDIR` (per-user) | `%LOCALAPPDATA%\Deskmate\run` with a user-only ACL |
| Local IPC endpoint | Unix domain socket | Unix domain socket | Named pipe, or Unix domain socket (supported on current Windows) |
| User service | `systemctl --user` unit tied to `graphical-session.target` | `launchd` LaunchAgent (per-user) | Per-user logon task (Task Scheduler) or Startup entry |
| Run without a session (opt-in) | `loginctl enable-linger` | LaunchAgent with `KeepAlive` is session-bound; LaunchDaemon only with explicit consent | Per-user service or scheduled task "run whether logged on or not" |
| Single-instance lock | `flock` on a runtime-dir file | `flock` on a runtime-dir file | Named mutex in the `Local\` namespace |
| Shared inference (system service) | system unit | LaunchDaemon | Windows service |
| File ownership check | `st_uid` equals current uid | `st_uid` equals current uid | Owner SID equals current user's SID |
| Desktop sensing adapter | KWin script (current); other compositors by their own adapter | Accessibility / CoreGraphics adapter (not yet built) | Win32 / UI Automation adapter (not yet built) |

A platform is considered multi-user capable once it implements the user-service, local IPC (or TCP with token and per-user port), single-instance lock and ownership-check rows.

## 3. Alternatives Considered

### 3.1 One shared brain with hot-swap by active user

Rejected.

* It still needs a per-session overlay and adapter, so it saves nothing.
* It needs the brain to detect the foreground user and handle edge cases: fast user switching, multiple seats, remote sessions, lock screens, users logged in on a console only. Each platform exposes this differently, so the logic would be written and tested once per OS.
* Swapping means flushing state, cancelling in-flight LLM, TTS and Blender jobs, closing and reopening SQLite, and reloading config on every switch. Each step is a race. A mistake mixes one user's memories into another user's conversation.
* The brain would have to run with privileges to read every user's data, or as a system service with per-user impersonation, turning a user-space app into a security-sensitive multi-tenant service.

### 3.2 One brain per user, shared TCP port with a user-routing proxy

Rejected. It adds a daemon to route, duplicates what the OS already provides through local IPC permissions, and brings back the question of how the proxy knows who is calling.

### 3.3 Do nothing; document single-user only

Rejected. The default fixed port and unauthenticated loopback make a second user's install silently fail or, worse, silently connect to the first user's brain.

## 4. Consequences

### Positive

* Strong isolation of memories, credentials and avatars with no new application logic.
* Fast user switching works. Each user's companion keeps its own state.
* The brain stays single-owner. Config hot-reload, reflection and SQLite assumptions are untouched.
* The local IPC endpoint removes the cross-user risk that loopback TCP leaves open.
* The decision is expressed against the platform contract, so new platforms add a mapping row and not a new design.

### Negative / Costs

* Memory and CPU scale with the number of simultaneously logged-in users (one Python daemon and one overlay each). Shared inference servers keep the dominant cost (model weights) constant.
* A second install step: a system-level inference service plus a per-user setup.
* More transport code: a local IPC listener in the brain and a matching connector in the overlay client, per platform.
* No single place to see "all companions on this machine". That is acceptable and avoids a cross-user surface.

### Out of Scope

* **Profile switching within one user** (several characters taking turns). That is ADR_PERSONALITY_PROFILES.md.
* **A shared appliance or kiosk** with one persistent character used by several people. That would be a profile feature inside one session and needs its own decision on whose memories apply.
* **Remote and mobile clients.** Covered by ADR_NETWORK_AND_MOBILE_STRATEGY.md. The token and per-user port rules here apply to them.
* **Writing the macOS and Windows adapters and service definitions.** Section 2.8 defines what they must provide. Building them is tracked with each platform's V-milestone.

## 5. Implementation Plan

1. **Audit** `brain/config.py`, `brain/main.py`, `brain/ipc/server.py`, `status_page/` and the install scripts for fixed ports, shared paths, machine-wide writes and POSIX-only assumptions. Add tests that two `Settings` with different `deploy_dir` and `port` run side by side.
2. **Platform path layer**: one small module that returns the per-user config, state and runtime dirs per platform, replacing direct `XDG_*` use in `brain/config.py`.
3. **Single-instance lock** in `brain/main.py` startup, behind a small cross-platform helper.
4. **Token auth**: implement `auth_token_file` (generate, read, enforce). Require it whenever TCP is used and more than one local account exists. Add tests for missing and wrong tokens on `/ws/overlay` and `/chat`.
5. **Local IPC transport** in the brain server and the overlay client, made the default under the user service. Linux first (Unix socket), with the transport behind an interface so other platforms plug in.
6. **Dynamic port** option (`port = 0` plus a port file) for TCP, read by the overlay and status page.
7. **Ownership check** helper (uid on POSIX, SID on Windows) applied to `deploy_dir` and `workspace_dir`.
8. **Install and service definitions**: extend the Linux install script for per-user config and optional lingering, and document the system-level inference service. Define the equivalents for other platforms as those ports land.
9. **Docs**: update README networking line, SPEC_BRAIN, SPEC_OVERVIEW and `config.example.toml` so they do not claim a fixed `8765` for the multi-user case.
10. **Manual test matrix**: two users logged in at once, fast user switch, logout and login, reboot, second brain start attempt, one user's client attempting the other's endpoint. Run on each supported platform.

## 6. Open Questions

* Should the local IPC endpoint become the default for all installs, or only when more than one local account exists? A single default is simpler to document and test. This ADR leans toward one default.
* Is a machine-wide limit on concurrent Blender builds needed in practice, or are per-brain limits enough?
* How should the shared inference service be packaged per platform: a service definition shipped by `deploy/`, or a documented recipe for the user's own Ollama/llama.cpp install?
* Do we support multiple simultaneous interactive sessions (multiple seats, remote desktop alongside a console) in V1, or only one active graphical session plus background sessions?
* Does the overlay's client toolkit support the local IPC endpoint on every target platform, or do some platforms need the TCP-with-token path as the long-term default?
