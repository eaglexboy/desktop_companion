# Spec: Resource sidecar and resource history

How the Admin app shows memory, CPU, GPU and thread use over time, for every machine in an instance, and how to set that up for yours.

## How it fits together

```
 machine A (the status page)            machine B (say, your LLM)         machine C (say, Windows)
 ┌──────────────────────────┐           ┌────────────────────────┐        ┌────────────────────────┐
 │ resources sidecar  :8781 │◄──┐       │ resources sidecar :8781│◄─┐     │ resources sidecar :8781│◄─┐
 └──────────────────────────┘   │       └────────────────────────┘  │     └────────────────────────┘  │
 ┌──────────────────────────┐   │ pull every 30 s, one token each   │                                 │
 │ Admin app (status page)  │───┴───────────────────────────────────┴─────────────────────────────────┘
 │ stores history, draws it │
 └──────────────────────────┘
```

* **The Admin app measures nothing itself.** It pulls from each *source* in `[status.resources.sources]`, stores what it gets in `<workspace_dir>/resources.db`, and draws it: current values on the Overview, history on the Resources page. Because it reads no process table, it runs on any OS.
* **Every machine, the Admin app's own included, runs a sidecar.** A sidecar knows how to read *its* OS and answers one question: what are the followed processes using right now? So the capability exists on every OS, and a machine or OS nobody has covered only needs a sidecar that speaks the contract below.
* **What is followed is configuration, never code.** Each sidecar's config says which processes to follow; the Admin app's config says which sidecars to ask. Nothing about your machines belongs in the repository.
* **Series are named `<source>/<process>`** (`workstation/deskmate-overlay`, `llm-box/vllm`), and a machine's GPU is `<source>/gpu card`.

## The contract

A sidecar is an HTTP server. Anything that implements this can be a source: the reference sidecar, a PowerShell script, a Go binary on a NAS.

### `GET /samples`

Request header, when the sidecar has clients: `Authorization: Bearer <token>`. Answer `200`, `application/json`, at most 1 MB:

```json
{
  "version": 1,
  "now": 1763300000.5,
  "rows": [
    {"proc": "vllm", "pid": 3149, "rss_mb": 26339.9, "anon_mb": 8810.1, "threads": 185,
     "cpu_pct": 101.5, "gpu_mb": 31846.7},
    {"proc": "gpu card", "gpu_mb": 31887.0, "gpu_util_pct": 22.0, "gpu_temp_c": 78.0, "gpu_power_w": 251.0}
  ]
}
```

| Field | Meaning |
| --- | --- |
| `proc` (required) | The series name: stable across restarts of the process, never containing `/`. The Admin app prefixes the source's name. |
| `pid` | The process id **as of this sample**. A change from one sample to the next is how a restart is detected: the Admin app marks it on the chart and does not call the drop in memory a release. Use any integer that changes when the thing restarts (a container's init pid is fine). Omit for the card. |
| `rss_mb` | Resident memory, MB. |
| `anon_mb` | **Private** memory, MB: what the process alone holds, excluding shared libraries and file cache. This is the number a leak shows in, and the one the Overview trends. Linux: `Anonymous` from `smaps_rollup`; Windows: private working set; macOS: unique set size. |
| `pss_mb` | Proportional memory, MB (Linux). |
| `threads`, `fds` | Thread count; open files (handles on Windows). |
| `cpu_pct` | CPU use over the time since the previous sample, in percent of **one core** (so 101.5 means a little over one core). Omit on the first sample. |
| `gpu_mb` | GPU memory held by this process, MB. |
| `gpu_util_pct`, `gpu_temp_c`, `gpu_power_w` | The whole card's utilization, temperature and power, only on the `gpu card` row. `gpu_mb` on that row is the whole card's memory in use. |

Rules:

* **Every field but `proc` is optional.** Leave out what you cannot measure (or send `null`); never send `0` for "unknown", because the history would show a flat line that is a lie.
* A followed process that is not running **has no row**. Do not send zeros for it.
* Unknown fields are ignored, so a sidecar may carry more.
* Numbers are plain JSON numbers; MB means 1024 × 1024 bytes.
* Answer within about 5 s (the Admin app's default timeout). If reading is slow, cache for a few seconds.
* A wrong or missing token is `401`. `GET /health` answers `{"ok": true}` (also behind the token when there are clients).

The Admin app asks every 30 s by default (`interval_s`). The sidecar keeps **no history**; the Admin app stores what it gets.

## The reference sidecar

`status_page/resources_agent.py` with `status_page/resources_collect.py`. Standard library only (Python 3.11+), plus `psutil` on Windows and macOS. It needs three files: `status_page/__init__.py`, `resources_collect.py` and `resources_agent.py`.

```
python -m status_page.resources_agent --config ~/.config/deskmate/resources-agent.toml
python -m status_page.resources_agent --new-token NAME     # a token for a client (see below)
```

Config (copy `resources-agent.example.toml`, keep your copy out of the repository):

```toml
host = "127.0.0.1"      # the machine's LAN address to be reached from other machines
port = 8781
backend = "auto"        # linux (/proc, cgroups; no dependencies) | psutil (pip install psutil) | auto
gpu = "auto"            # nvidia | xpu-smi | none | auto (nvidia if nvidia-smi is installed)
# gpu_container = "..." # xpu-smi runs inside this Docker container

[client.my-status-page]                 # one table per status page that may ask
token_sha256 = "…"                      # from --new-token; the sidecar keeps only the hash

[process.overlay]                       # one table per process; its name is the series name
unit = "deskmate-overlay"               # exactly one of: unit | port | match | container
[process.tts]
port = 7501                             # whatever listens on this local TCP port
[process.worker]
match = "my_worker\\.py"                # a regex over the command line (lowest matching pid)
[process.llm]
container = "my-inference-container"    # a Docker container: everything in it counts
```

| Target | Finds | Notes |
| --- | --- | --- |
| `unit` | a systemd **user** unit's main process | Linux only. If the unit wraps the real process in a shell script, follow it by `port` or `match` instead. |
| `port` | the process listening on a local TCP port | Linux: only your own processes unless the sidecar runs as root. |
| `match` | the lowest pid whose command line matches the regex | Never matches the sidecar itself. Make it specific. |
| `container` | a Docker container | Linux: read from the container's cgroup (memory includes file cache; `anon_mb` is its private part). Elsewhere: from `docker stats`. |

The same numbers come from either backend, with small honest differences (Windows has no proportional memory; `anon_mb` is the OS's closest private figure). Mixing OSes in one instance is fine because each series is its own line.

### GPU

* **NVIDIA** (`gpu = "nvidia"`, any OS): `nvidia-smi` gives the card's memory, utilization, temperature and power, and the memory each process holds. Where the driver does not report per-process memory (Windows WDDM), only the card is shown.
* **Intel** (`gpu = "xpu-smi"` with `gpu_container`): `xpu-smi` usually exists only inside a vendor container; the sidecar runs it with `docker exec`. The container named there gets the memory `xpu-smi ps` reports for it; other processes get none.
* **Anything else** (`gpu = "none"`): no GPU rows. A different GPU tool is a different `gpu` provider in `resources_collect.read_gpu`, or your own sidecar.

## Tokens: one per client

A *client* is one status page instance asking a sidecar. Each client has its own token, so a laptop's can be revoked without touching the desktop's, and a leak on one is contained.

**Make a token** on the machine that runs the sidecar:

```
$ python -m status_page.resources_agent --new-token windows-desktop
Token for client 'windows-desktop'. Give it to that client now; it is not stored anywhere here:

    k3J...(43 characters)...Q

Add this to the sidecar's config (it holds only a hash) and restart the sidecar:

[client.windows-desktop]
token_sha256 = "9f2c…"
```

1. Paste the `[client.…]` table into the sidecar's config and restart the sidecar. It holds **only the hash**, so reading its config does not reveal the token.
2. Give the token itself to that client, once, over a channel you trust. Nothing stores it for you: if it is lost, make a new one.
3. On the client, keep it in the environment, not in `config.toml`: put `NAME=token` in a file only you can read (`chmod 600`), have the Admin app's service load it (systemd `EnvironmentFile=`, or a Windows service/Task environment variable), and refer to it as `env:NAME` in the config below.

**Rotate or revoke:** make a new token for the client and replace its table (rotate), or delete its table (revoke); then restart the sidecar. Other clients are unaffected. The sidecar prints `client <name> connected from <address>` the first time each client asks after it starts, so the journal shows which are in use.

**No clients configured:** allowed only while the sidecar listens on loopback (`127.0.0.1`), where only this machine can reach it. Listening on any other address with no `[client.*]` is refused unless you set `open = true` (when a firewall is your protection). Token comparison is constant-time. Always use a firewall too: allow the sidecar's port only from the machines that need it.

## Telling the Admin app

In the Admin app's `config.toml` (your own copy; `config.example.toml` has the commented template):

```toml
[status.resources]
# enabled = true
# interval_s = 30          # how often to ask
# retention_days = 14      # how long history is kept

[status.resources.sources.workstation]      # the name becomes the series prefix
url = "http://127.0.0.1:8781"               # the sidecar on this machine
token = "env:STATUS_TOKEN_LOCAL"            # blank is fine only for a loopback sidecar with no clients

[status.resources.sources.llm-box]
url = "http://192.168.0.21:8781"
token = "env:LLM_BOX_RESOURCES_TOKEN"
```

Restart the Admin app after changing it. A source that does not answer shows a red note on the Overview ("sidecar unreachable") and its history simply has a gap.

## Setting up a machine

Do this for each machine, the one running the Admin app included.

1. **Copy the three files** (`status_page/__init__.py`, `resources_collect.py`, `resources_agent.py`) to the machine, or use a checkout of the repository. On Windows and macOS, `pip install psutil` (the Admin app's own venv already has what the Admin app needs; the sidecar needs only Python 3.11+).
2. **Write its config** from `resources-agent.example.toml`: the host to listen on, one `[process.*]` per thing to follow, `gpu`.
3. **Make a token** for each status page that will ask (`--new-token`) and paste the tables in.
4. **Run it and check it** (below), then **keep it running** with the machine's service manager, separate from whatever it watches so it keeps reporting while that is restarted or down:
   * **Linux (systemd user unit):** `deploy/systemd/deskmate-resources-agent.service` is the template (`deploy/install.sh` installs it with the paths filled in for the machine with the repository; edit the two paths by hand elsewhere). `systemctl --user enable --now deskmate-resources-agent`. A user unit stops at logout unless `loginctl enable-linger $USER` is set.
   * **Windows:** Task Scheduler, "At startup" (or "At log on"), action `python.exe -m status_page.resources_agent --config C:\path\resources-agent.toml`, start in the folder holding `status_page\`, "Run whether the user is logged on or not", with the token variables set for that task (or a service wrapper such as NSSM). Allow the port in Windows Defender Firewall for the status page's address only.
   * **macOS:** a launchd agent in `~/Library/LaunchAgents` with `ProgramArguments` as above, `KeepAlive` true, and the token variables under `EnvironmentVariables`.
5. **Open the port** to the status page's address only (`ufw allow from <address> to any port 8781`, or the platform's equivalent). Listening on loopback needs no firewall rule.

**Check it** from the Admin app's machine:

```
curl -s -H "Authorization: Bearer $TOKEN" http://<sidecar>:8781/samples
```

You should see a row per followed process; no token or a wrong one is `401`. Then add the source to the Admin app's config and restart it: the machine appears on the Overview within a minute.

### Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| "sidecar unreachable (URLError …)" | The sidecar is not running, the address or port is wrong, or a firewall is in the way. |
| "sidecar unreachable (HTTPError: HTTP Error 401…)" | The token the Admin app sends is not one of the sidecar's clients: re-make it, or the environment variable was not loaded (restart the Admin app). |
| A process has no tile | It is not running, or the target does not find it. Try `match`/`port` instead of `unit`; on Linux a `port` target only sees your own processes. |
| Private memory is missing | The OS refused `psutil` the detail for that process (run the sidecar as the same user, or elevated for system processes). |
| The sidecar refuses to start | Its message says why: usually an unknown setting, a `[process.*]` with two ways of finding the process, or a non-loopback address with no client. |

## Writing your own sidecar

For an OS or device the reference sidecar does not cover (a router, a NAS, another language): implement `GET /samples` and the optional bearer-token check above, and nothing else. Keep it read-only, keep it to the machine's own numbers, answer from a cache of a few seconds if reading is expensive, and follow the rules for absent values. To compare notes, point the Admin app at it as a source and look at the Resources page, or check the response against the table above with `curl`.

## What the Admin app serves

| Route | Purpose |
| --- | --- |
| `GET /api/overview` | Also carries `resources` (each series' latest sample, with `anon_change_6h`: how its private memory moved over the last 6 hours, or null if it was restarted in that time) and `resource_sources` (per source: whether the last pull worked). |
| `GET /api/resources?metric=&hours=` | One metric over time per series: `metric` is one of `anon`, `rss`, `pss`, `gpu`, `gpu_util`, `gpu_temp`, `gpu_power`, `threads`, `fds`, `cpu`; `hours` is clamped to the retention. Thinned to at most 600 points per series by averaging, with `restarts` (`[ts, series, pid]`) marking a pid change. |

## Tests

`brain/tests/test_resources.py` (the store, the puller, the routes, the config) and `brain/tests/test_resources_sidecar.py` (reading, the config, per-client tokens and what the sidecar serves). The `psutil` test is skipped where `psutil` is not installed.
