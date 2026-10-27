# Architecture Decision Record: Model Context Protocol (MCP) Client Integration

* **Status:** Proposed. Deferred to V2+ (Phase 2 of [ADR_SKILLS_AND_AGENT_ORCHESTRATION.md](ADR_SKILLS_AND_AGENT_ORCHESTRATION.md)); nothing in V1 depends on it.
* **Date:** 2026-10-26
* **Deciders:** Architecture & Core Engineering Team
* **Related:** [ADR_SKILLS_AND_AGENT_ORCHESTRATION.md](ADR_SKILLS_AND_AGENT_ORCHESTRATION.md)

---

## 1. Context

Agent Skills (`SKILL.md`) give the companion *instructions*. They do not give it *capabilities*: a skill that says "look up the user's calendar" needs a tool that can. [MCP](https://modelcontextprotocol.io) is the open protocol for exposing such tools (and resources and prompts) from separate server processes: calendars, files, browsers, home automation, databases. Adopting it means we write no per-integration code, in the same way adopting `SKILL.md` means we write no per-skill code.

The ADR on skills already reserves the hook: `register_tool(name, description, parameters, handler)` in `router.py`. MCP tools are the first thing that hook is for.

## 2. Decision

1. **Add an MCP *client* in V2+**, not before. It lives in `brain/mcp/`, and the companion is never an MCP server in this ADR (a possible later one).
2. **Every MCP tool is a Track 2 (`tool`) capability registered through the existing registry**, so the reply format, argument validation, per-call timeout, usage counters and status-page view all apply unchanged. The model proposes; the registry disposes.
3. **Servers are declared by the user in config; they are never discovered or installed by a skill.** A skill may *name* the tool it needs; it cannot add a server.
4. **Prefer the official `mcp` Python SDK** over a hand-written client. [OpenJarvis](https://openjarvis.stanford.edu/) ships a small hand-rolled client (stdio + streamable-HTTP transports, JSON-RPC, protocol `2025-03-26`) that is a useful reference for the handshake and transport edge cases, but a protocol client is maintenance we should not own. Evaluate the SDK first; adapt OpenJarvis code only for gaps, with attribution in `NOTICE`.
5. **Local servers run sandboxed.** A stdio server is an arbitrary process launched on the user's desktop; it runs under the same Bubblewrap policy as Track 1 scripts, with its network and filesystem grants declared per server.

## 3. Design

### 3.1 Configuration

```toml
[mcp]
enabled = false                          # off by default, even once implemented
call_timeout_s = 30.0                    # per tool call; the typed-action default (8s) is too short for many servers
max_result_chars = 8000                  # results are truncated before reaching the model

[mcp.servers.calendar]
transport = "stdio"                      # "stdio" | "http"
command = ["uvx", "calendar-mcp"]        # stdio only
url = ""                                 # http only (https required unless loopback)
env_passthrough = ["CALENDAR_TOKEN"]     # nothing else is inherited
allow_network = true
allow_filesystem_read = ["~/Calendars"]
tools = ["list_events", "create_event"]  # allowlist; empty = none. There is no implicit "all".
confirm = ["create_event"]               # tools needing per-call user approval
```

### 3.2 Lifecycle

* Servers start lazily on first use and stop after an idle period, so a quiet companion holds no stray processes or VRAM.
* A crashed or hung server marks its tools unavailable and emits `skill.rejected`-style events on the status stream; it never blocks a reply. Reconnect uses backoff.
* All calls are `asyncio`; the blocking-thread client approach used by OpenJarvis is not suitable for our event loop.

### 3.3 Tool exposure to the model

Prompt space on a small local model is scarce. Only the **allowlisted** tools of **enabled** servers are rendered into the skills block, as one line each, exactly like typed actions (`name(arg: text): description`). MCP `inputSchema` is mapped to our `Param` list; a tool whose schema cannot be expressed (nested objects, arrays) is not exposed rather than approximated.

## 4. Security (the reason this is an ADR and not a ticket)

MCP moves trust decisions from our code to someone else's server, and tool metadata is attacker-controlled text that reaches the model.

| Threat | Mitigation |
|---|---|
| **Tool poisoning**: a server's tool *description* contains instructions aimed at the model | Descriptions are scanned with the importer's injection scan (`brain/skills/safety.py`) and capped in length; flagged tools are not exposed. The allowlist means unknown tools never reach the prompt. |
| **Result injection**: a tool returns text that tells the model to act | Results are data, never re-interpreted as the reply format. They are fed back, truncated, wrapped as quoted tool output. Skills proposed from inside a tool result are dropped. |
| **Rug-pull**: a server changes its tool list after approval | Allowlist is by name *and* a hash of the schema; a change disables the tool until the user re-approves. |
| **Over-broad grants** | Per-server sandbox grants; per-tool `confirm` uses the existing `agent_permission_request` flow (allow once / always / deny, 60s timeout defaults to deny). |
| **Credential leakage** | Servers receive only `env_passthrough` variables. Secrets live in the environment, never in `config.toml` or the status page. |
| **Remote servers** | https only (loopback excepted); no credentials are sent to a URL the user did not write. |
| **Starvation** | Calls to remote servers have timeouts and a concurrency cap; they never hold an ICG inference slot, because they do not use inference. |

## 5. Interaction with Agent Skills

* A skill's `allowed-tools` and body may refer to tools by name. Mapping those names (Claude Code's `Bash`, `Read`, `WebFetch`, or an MCP `server.tool`) onto what this installation actually offers is the **tool-translation layer** owned by the Phase 2 agent runtime. A reference-only table exists in OpenJarvis (`skills/tool_translator.py`).
* A skill whose required tools are not available is shown as `unavailable: needs <tool>`, not silently degraded.

## 6. Alternatives Considered

* **Hand-written integrations per service.** Rejected: unbounded maintenance, and users already have MCP servers.
* **Embed OpenJarvis wholesale to get its MCP support.** Rejected for this purpose: it overlaps our LLM pool, voice stack and scheduler, and brings a Rust extension and a Node frontend. Its client is ~150 lines plus transports; the SDK covers it.
* **Let skills install MCP servers.** Rejected: that is remote code execution by design.

## 7. Exit Criteria (for the future implementation)

* With `[mcp] enabled = false` (the default) no MCP code is imported and no process is started.
* A configured stdio server starts under Bubblewrap with only its declared grants; tools outside the allowlist cannot be called by any means, including a model that names them.
* A poisoned tool description and a poisoned result each fail a test.
* A schema change disables the tool until re-approved.
* A hung server does not delay a reply by more than `call_timeout_s`, and never blocks the voice loop.
* The status page lists servers, their tools, state, and per-tool call/failure counters.

## 8. V1 Accommodations (already present; nothing new is required)

* The registry's `Skill`/`Param`/`validate`/`run_all` path is the integration point.
* `[skills]` config and the `skill.call` / `skill.rejected` events carry MCP calls unchanged.
* The IPC permission envelopes (`agent_permission_request` / `_response`) are reserved.
