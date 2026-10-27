# Spec: Status Page (`status_page/`)

## Role

A browser dashboard for seeing what the stack is doing without a terminal tab per service: health, the live decision pipeline, mood over time, chat and logs. It is a **separate process** from the brain (`deskmate-status.service`, `python -m status_page.app`), deliberately: it must stay up and restartable when the brain hangs or crashes, it cannot slow the brain, and iterating on it never interrupts the companion.

## Principles

* **Read-only.** It never writes `state.db` (opened `?mode=ro`), never controls services (only queries `systemctl --user is-active/show`), and never executes anything the browser names.
* **Local only.** Binds `[status] host:port` (default `127.0.0.1:8780`); a middleware rejects any request whose `Host` header is not that address (DNS-rebinding guard). The page shows chat history and prompts, so do not bind it beyond loopback.
* **Degrades visibly.** A brain that cannot be reached is a banner and a red dot, not an empty page; the rest (units, endpoint probes, database, logs) keeps working.
* **Config:** the daemon's own `config.toml` (`get_settings()`), plus `[status]`: `host`, `port`, `units` (journalctl `--user` units shown as log panes), `extra_logs` (name $\to$ file path).

## Endpoints

| Route | Purpose |
| --- | --- |
| `GET /` | `static/index.html`: markup only. |
| `GET /static/...` | The UI's CSS and ES modules (`StaticFiles`, mounted last so it never shadows an `/api` route; covered by the same Host-header guard). No build step, no external assets. |
| `GET /api/overview` | One poll (the page polls every 3 s): unit states, backup status (`[storage] backup_dir`'s `state.db` mtime plus `deskmate-backup.timer`), brain `GET /health` + `GET /api/status` + `GET /api/skills`, endpoint probes, and from `state.db` the last 40 chat turns (each assistant turn joined to its `reply_raw` row, when it has one; a database from before that table still reads), 12 memory notes, `agent_state`, traits and mood. |
| `GET /api/events` | Relays the brain's SSE event stream (`GET /api/events`, see [SPEC_BRAIN.md](SPEC_BRAIN.md) "Event Stream") so the browser talks only to this service. `?since=` / `Last-Event-ID` pass through; if the brain is unreachable it emits a `brain-down` event and ends, and the browser's `EventSource` retries. |
| `GET /api/history?kind=mood\|traits&hours=` | Time series from `personality_history`, step-function points per name, down-sampled to $\le 600$ per series, anchored by the last point before the window (1 hour to 30 days). |
| `GET /api/logs/{source}` | SSE tail of one configured source: `journalctl --user -u <unit>.service -o short-iso -n 200 -f`, or `tail -F -n 200 <file>`. The client names a source; it can never supply a command or path (unknown $\to$ 404). |

Endpoint probes (`/models` of the local and classifier LLMs; each `[voice.tts.remote.*]` instance's capabilities endpoint) use a 2 s timeout; disabled instances show as such rather than failing.

## Sizing

Every length is in `rem` and lives in `css/`, so the page scales with the viewer's root font size and browser zoom. The JavaScript carries **no size values**: swatch shapes are CSS classes (`.sw`, `.sw.dot`, `.sw.box`), and the chart tooltip is placed by CSS (`.tip`) from two custom properties, `--tip-x`/`--tip-y`, that the script sets to the measured cursor position (a coordinate, not a design size, so it is the one place a pixel value appears); the gap from the cursor and the side it sits on (`.flip-x`/`.flip-y`, toggled when a measurement shows it would leave the chart) are CSS. Two tests enforce this: no `px` length in `css/`, and no size-bearing inline style in `js/`. The chart's SVG user units (its `viewBox`) are unitless geometry, not CSS sizes.

## Source layout

```
status_page/
├── app.py                  # FastAPI service (routes above)
└── static/
    ├── index.html          # markup only; links the CSS files and js/main.js (type="module")
    ├── css/
    │   ├── tokens.css      # colors for light and dark; series slots --s1..--s3
    │   ├── base.css        # reset, header, tab nav, buttons, tab panels, section cards
    │   ├── components.css  # status rows, dots, key/value, bars, chips, tables, banner, empty state
    │   ├── thinking.css  timeline.css  chat.css  logs.css   # one per tab
    └── js/
        ├── main.js         # wires the modules together and starts the poll and the stream
        ├── bus.js          # pub/sub ("overview", "stream", "tab", "theme"): modules never import each other's panels
        ├── util.js         # $, esc, time/format helpers, storage and JSON helpers
        ├── overview.js     # polls /api/overview; Overview tab, problems banner, memories, chat
        ├── diff.js         # raw model reply vs saved text: dialogue extraction, word diff, comparison block (pure functions; used by thinking.js and overview.js)
        ├── stream.js       # the brain's event stream -> bus
        ├── personality.js  # live mood/trait bars          feelings.js  # feeling() and its colors
        ├── thinking.js     # per-turn cards                skills.js    # registry, activity, prompt block
        ├── timeline.js     # chart                         series.js    # step-series lookup
        ├── logs.js         # log tails                     tabs.js  theme.js
```

Each panel module exports one `initX()` and reacts to bus events; adding a panel means a new module, a new `initX()` call in `main.js`, and (for a tab) an entry in `tabs.js` and `index.html`.

## UI

Six tabs (location hash), a live-connection indicator, a light/dark toggle (`prefers-color-scheme` plus an explicit override; chart colors are the validated categorical slots 1-3 for both modes), and a problems banner (a unit not active, brain/LLM/TTS unreachable, overlay not connected, database error).

* **Overview:** services, endpoints, companion (name, voice, presence, look, overlay connection, uptime), **backup** (target, age of the last good snapshot, timer state; a problem is raised if no `backup_dir` is configured, no backup has been made, the last is over 36 h old, or the timer is not active), "feeling now", live mood and trait bars with the last change, placement (avatar, active window, screens).
* **Thinking:** a feed of cards, one per user turn (events grouped by their `turn` id): the prompt (collapsed), routing decision and reason, the pool chosen (including "wanted cloud, none configured, fell back"), each model call with latency and tokens, the model's reasoning when `[llm.local] reasoning = true`, reply format (flagging an unstructured reply), skills run (`Did:`) or refused (`Refused:`), the **raw response** (the model's reply exactly as returned, from the `reply.raw` event, with a word diff against the text that was finally said and saved), the spoken reply, and per-sentence voice timing. Reflection results appear as their own cards; a "Background events" toggle adds placement moves and mood shifts. A note states whether reasoning is on, since it is off by default.
* **Skills:** every registered skill (enabled or not) with its description, arguments (enum values shown), state, and in-memory usage counters (calls, failures, refusals, last used, last error; reset on brain restart), a live list of skills run or refused since the page opened, and the exact skills block appended to the model's system prompt (the only way the companion knows what it can do). An **Installed Agent Skills** table lists third-party `SKILL.md` skills: display name and canonical `source/name` id, purpose, trust tier, enabled / pinned / exposure (`absent`, `discoverable`, `stub`) with the stub's token cost, source, commit and category, license, notes (scripts, dangerous tools, scan findings), and whether it runs (not yet: the agent runtime does not exist, and no skill is put in the prompt). The heading carries the budget meter (enabled and pinned counts, stub tokens against the budget, the context size it was derived from). Rejected directories follow, with the reason. The table is **read-only**, like the rest of the page; changes are made with `python -m brain.skills.manage` or the brain's `POST /api/skills/state`. Data comes from the brain's `GET /api/skills`, fetched with each overview poll.
* **Mood timeline:** valence/energy/stress (or up to three chosen traits) as step lines over a selectable range, hover crosshair with values, a table view, and a strip of derived *feelings* along the bottom. The feeling is computed in the page from the three mood values (stress $\ge 65$ nervous/anxious; valence $\ge 65$ excited/happy; $\le 35$ sad/frustrated; then sleepy/energetic by energy; else content/down/neutral); it is never stored. Live `personality.*` events extend the chart without a reload.
* **Chat & memory, Logs:** recent turns with provider/escalation, each assistant turn that has a recorded raw reply carrying a collapsible "raw response" with the same comparison (the diff is between the dialogue the model produced — the `say` of a structured reply, else the whole text — and the saved text, so it isolates what the cleanup removed or added; open comparisons stay open across the 3 s re-render), memory notes; log panes per source with filter, pause and clear (a dropped log stream is re-opened rather than auto-resumed, to avoid replaying the backlog as duplicates).

## Deployment

`deploy/systemd/deskmate-status.service` (placeholders `@REPO_ROOT@`, `@VENV_PYTHON@`, as the brain's unit); installed by `deploy/install.sh`, enabled with `systemctl --user enable --now deskmate-status.service`. It is `WantedBy=default.target` and deliberately not tied to the brain or the graphical session.

## Tests

`brain/tests/test_status_page.py`: Host-header guard (including on `/static`), log-source whitelisting, read-only database access, missing-database handling, every asset the index references is served, every JS `import` resolves, and the static mount cannot serve files outside its directory.
