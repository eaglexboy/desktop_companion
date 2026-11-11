# Architecture Decision Record: Remote Model Processing & Rendering (Distributed Setups)

* **Status:** Proposed. **Not implemented**; written so that it can be, later, without rework.
* **Date:** 2026-11-06
* **Deciders:** Architecture & Core Engineering Team
* **Related:** [ADR_NETWORK_AND_MOBILE_STRATEGY.md](ADR_NETWORK_AND_MOBILE_STRATEGY.md) (remote clients, auth, HTTP asset delivery), [SPEC_BLENDER.md](SPEC_BLENDER.md) (the generator and its contracts), [ADR_ACCESSORY_MODELS.md](ADR_ACCESSORY_MODELS.md) (decisions 8, 14-18: conversion, libraries, retargeting), [ADR_LOOK_HISTORY.md](ADR_LOOK_HISTORY.md) (thumbnails, rebuild from spec), [SPEC_BRAIN.md](SPEC_BRAIN.md) (the pools for remote LLMs and speech)

---

## 1. Context

A user may split the stack across machines: the companion's overlay on the desktop in front of them, the model work on a machine with more cores and memory, the language model on another, speech on a third. Two of those splits already exist: remote LLMs (`[llm.cloud.<name>]`, `[llm.local].base_url`) and remote speech (`[voice.tts.remote.<name>]`, `[voice.stt.remote]`) are configured as endpoints, picked by priority, health-checked and failed over. A remote *client* (a second overlay, a phone) is the network ADR's Phase 2 and 3. **The model work has no such split: the brain runs Blender itself, on its own machine, from its own disk.**

"The model work" is everything the project does with Blender:

| Job | Today | Input | Output |
|---|---|---|---|
| **build** a look | `blender_gen._run_blender`, `blender/generator.py --spec`, 160 s timeout, one at a time (the build lock) | the `LookSpec`, and from the deploy directory the body, the outfit and accessory models, the catalogs and the animation libraries | `look-vN.glb`, `build.json` |
| **thumbnail** a look | `blender/thumbnail.py`, Workbench engine, about 2 s, no GPU | a `.glb` | a PNG |
| **convert** a model on install | `generator.py --convert`, `blender/sketchfab_convert.py`, `blender/mixamo_convert.py body` | a download, up to hundreds of MB | a clean `.glb` |
| **inspect** a body | `generator.py --inspect` | a `.glb` | sockets, bones, capabilities (`.caps.json`) |
| **retarget** libraries | `blender/rig_convert.py clips`, `blender/mixamo_convert.py clips` | a body and a library | a library for that body |

Four things make this worth splitting, in order of weight:

1. **Memory and time.** A look build with the animation libraries measured 110 s, and 106 s before the last clips were added, against what was then a 120 s timeout (now 160 s). Cleaning up one Sketchfab download meant reading a 254 MB file (Blender holds a model decoded, many times its size; not measured here). The companion machine is also the user's desktop, which these jobs compete with.
2. **Untrusted input.** Downloaded models are parsed by Blender. On a worker, a hostile file meets a machine that holds nothing of the user's; today it meets their desktop and their database.
3. **The companion machine may not have Blender at all** (a thin laptop, a mini PC), and should not have to.
4. **The pieces are meant to speak HTTP.** The brain announces a look to the overlay with both a local path and a URL (`asset_dir` in `welcome`, `glb_url` = `/assets/<file>` in `hot_reload_asset`: the network ADR's "no shared filesystem" rule), so a remote overlay is designed for. Today `/assets/` serves the loopback; its authenticated, non-loopback form is that ADR's Phase 2, which the worker's file transfer below builds on.

**What "rendering" means here.** Blender renders only the thumbnails (Workbench, CPU, 2 s). The avatar the user sees is rendered by the overlay (Qt Quick 3D) on the machine it runs on, from the `.glb`. So "remote rendering" is two different things, decided separately below: *offline processing and rendering in Blender* (this ADR's decision) and *streaming the live avatar as video* (considered and not chosen).

## 2. Decision

1. **A remote worker runs the same Blender jobs, under the same contracts.** A *worker* is a small stateless HTTP service on a machine with Blender (`python -m worker`), started from the same repository revision as the brain. It runs the existing scripts, unchanged, as subprocesses, and returns what they wrote. The brain's code above the subprocess (build lock, publish, manifest, prune, thumbnails, rollback) does not change.
2. **The brain gets a `Runner` seam and two implementations.** `_run_blender` and the other direct subprocess calls become calls on a runner: `LocalRunner` (what runs today, and the default) and `RemoteRunner` (the worker protocol below). A worker pool is configured like the speech pools: keyed tables, `priority`, health, failover. With `local_fallback = true` (the default when Blender is on `PATH`) a worker that is down or too slow means the job runs locally; without Blender locally, the failure takes the path a failed build takes today (the look stays as it was, the user is told).
3. **The brain stays the single authority and the workers stay stateless.** Models, libraries, the manifest, credits, sidecars and every published `.glb` live in the brain's deploy directory. A worker holds a content-addressed cache that can be deleted at any time and nothing else; no database, no personality, no LLM, no user data beyond the files of the job.
4. **Assets move by hash, not by shared path.** A job names the files it needs as `{path: sha256, size}` (the same map as `bundle.json`'s `files`, ADR_ACCESSORY_MODELS 19). The worker asks for those it does not have (a `GET` from the brain's media endpoint, in its authenticated Phase 2 form from the network ADR; or the brain's upload, see open question 1), verifies each hash, caches them, and lays them out in a scratch deploy directory with the same relative paths, so `generator.py --deploy-dir` runs unmodified. The brain works out the list by resolving the spec the way the generator does (base template to its model file, wardrobe to outfit files, accessories, the library folder that body plays: `library_folder`), so a worker never receives the 254 MB source of a model that was cleaned up at install, or a library no look uses.
5. **Named job kinds only, never scripts.** The worker accepts `build`, `thumbnail`, `convert`, `inspect`, `retarget` and refuses anything else. The scripts are the ones in its own checkout. The brain sends data (a spec, hashes), never code. A worker reports its repository revision and Blender version in `/health`; the brain refuses a worker whose revision differs from its own (the scripts are a contract with `brain/`) and warns on a different Blender version (output differs a little between versions).
6. **The generator's contracts do not change.** A worker returns the process exit code, the tail of stdout and stderr, `build.json` and the artifacts; the brain parses `DESKMATE_BUILD_OK` / `DESKMATE_BUILD_FAIL` and the budgets (30,000 triangles, 15 MB) exactly as now, because they are enforced inside the script, which is the same script. Timeouts are the local ones plus a configured allowance for the transfer.
7. **Authentication and transport are the network ADR's.** Bearer token from `auth_token_file`; a worker binds to loopback unless a token is configured, as the brain does; on anything but a trusted LAN it is reached over the private mesh (Tailscale or WireGuard), never the open internet. Tokens are per worker.
8. **The live avatar is still rendered by the overlay, from the `.glb`.** Streaming rendered frames from a remote machine is not part of this decision (section 4).
9. **Observability is part of the work.** The status page gets a read-only *Workers* section: each worker's health, revision, Blender version, queue, the last job and how long it took, and its cache size; the manifest records, per version, where it was built.

### 2.1 The worker protocol (sketch)

```
GET  /health                  -> {"ok": true, "revision": "...", "blender": "5.0.1", "jobs": ["build", ...], "busy": 0, "cache_bytes": N}
POST /jobs                    -> 202 {"id": "...", "need": ["<sha256>", ...]}      body: {"kind": "build", "args": {...}, "files": {"assets/custom_templates/hero.glb": {"sha256": "...", "size": N}, ...}}
PUT  /blobs/<sha256>          -> 204                                               the brain uploads what the worker lacks (or the worker pulls it: see open question 1)
GET  /jobs/<id>               -> {"state": "queued|fetching|running|done|failed|cancelled", "returncode": 0, "stdout_tail": "...", "artifacts": [{"name": "look.glb", "sha256": "...", "size": N}]}
GET  /jobs/<id>/artifacts/<name>
DELETE /jobs/<id>             -> cancels (the SIGKILL and scratch discard `_run_blender` does today)
```

Configuration, in the style of `[voice.tts.remote.<name>]`:

```toml
[appearance]
local_fallback = true                    # run the job here if no worker can

[appearance.workers.bigbox]              # keyed: one table per worker, none by default
base_url = "https://100.64.0.7:8790"
auth_token_file = "~/.config/deskmate/worker-bigbox.token"
priority = 20                            # higher first, as for [llm.cloud.*] and [voice.tts.remote.*]
kinds = ["build", "thumbnail", "convert", "inspect", "retarget"]
max_concurrent = 2
transfer_allowance_s = 60                # added to the job's own timeout
```

## 3. Consequences

**Good**

* Blender, its memory and its parsing of untrusted files can leave the user's desktop; a companion machine without Blender works.
* Builds stop competing with the desktop and can take the time they need; the 160 s timeout is no longer a ceiling set by the weakest machine.
* The split is the one the user already has for LLM and speech: configured endpoints, priority, failover, a status view.
* Nothing about the avatar's behaviour, the manifest or the publish path changes; a worker is a different place to run the same subprocess.
* Single-machine users change nothing: the default is `LocalRunner`.

**Costs and risks**

* A new service to install, version-match and secure. The revision check and the single-owner token keep it honest, and it is an *optional* component.
* The first job on a cold worker moves tens of MB (a body, the libraries it plays); later jobs move only what changed. Libraries are the large part, and `library_folder` keeps each body to the libraries it actually plays.
* Builds are no longer byte-for-byte reproducible across machines if the Blender versions differ (the existing determinism is by seed on one install). Pinning the version in the worker's `/health` and warning on a mismatch bounds this; it is not eliminated.
* More failure modes: transfer cut short (hashes catch it), worker lost mid-job (the job is retried once, then falls back), a stale cache (content addressing makes a stale entry impossible, only an unused one), clock and revision skew (refused at `/health`).
* The brain must know what a job reads. Today the generator finds its files itself; the manifest of files must stay in step with it. A test that runs the generator against a deploy directory containing **only** the listed files (and fails on any other read) guards this.

## 4. Alternatives considered

1. **A shared filesystem and `ssh blender ...`.** The least code: mount the deploy directory on the worker, run Blender over SSH. Rejected as the design (kept as a documented way to try the idea): it needs the same paths on both machines, has no hash check, no cancellation worth the name, no health, no priority or failover, and puts the user's whole deploy directory (the database next to the assets) within the worker's reach.
2. **A general job queue (Celery, Redis, a broker).** More machinery than a single user's companion needs, and a broker is a new thing to run on every machine. The jobs are few, large and infrequent, which an HTTP service handles.
3. **Run the whole brain remotely and make the overlay a thin client.** This is the network ADR's Phase 2 and it stays available, but it moves everything or nothing: the user who wants only the heavy Blender work elsewhere (and the LLM somewhere else again) is not served.
4. **Stream the live avatar as video from a remote GPU.** Not chosen. The overlay is a transparent, click-through window composited on the desktop, which video does not give; lip-sync and gestures are driven by the overlay from visemes and clips with sub-frame timing; a remote GPU would need the same glTF loading the overlay already does; and the cost is a stream with latency and bandwidth in place of a 10 MB file fetched once. A remote client (the network ADR's web client) renders locally from the same `.glb`. Revisit only if a client appears that cannot render glTF.
5. **Pre-building looks and fetching them.** Looks are built on demand from a spec the companion chose a moment ago; there is nothing to pre-build.
6. **Cloud functions per job.** The jobs need Blender, hundreds of MB of input and minutes, and send a user's model files to a third party. Not impossible behind the same protocol (a worker can be anywhere), but not a design target.

## 5. Implementation plan (for later; each step ships on its own)

1. **The seam.** Introduce `Runner` and `LocalRunner`; route `_run_blender`, thumbnails, `convert_to_glb`, `_run_inspect`, `_generator_run` (the Mixamo, Sketchfab and rig conversions and the retargeting) through it. No behaviour change; the existing tests are the proof.
2. **The file manifest.** `required_files(spec, deploy_dir)` with the guard test above, and a content-addressed blob store on the brain's media endpoint.
3. **The worker.** `worker/` (`python -m worker`): `/health`, the job API, the cache with least-recently-used eviction by size, the revision and Blender checks, the token. Build and thumbnail jobs first.
4. **`RemoteRunner` and the pool.** Priority, health probing, failover, the local fallback, the timeout allowance, cancellation. Contract tests against an in-process fake worker; fault injection (a timeout, a bad hash, a revision mismatch, a worker that vanishes mid-job); a parity test that a build through a loopback worker and a local build agree on `build.json`'s structure, triangle count and the set of clips.
5. **Install-time jobs.** `convert`, `inspect` and `retarget` through the pool, with the upload of a large source (resumable, hash-checked) and `models ... --worker <name>` / `--local` to choose. Installation, credits and sidecars stay brain-side.
6. **Observability.** The Workers section on the status page; the build site in the manifest.
7. **Documentation.** SPEC_BLENDER (the runner contract), SPEC_BRAIN (the config tables), SPEC_STATUS_PAGE, and a how-to for the three splits (worker on a LAN machine; worker over the mesh; a thin companion machine with no Blender).

## 6. Open questions

1. **Push or pull for blobs.** The worker pulling from the brain's media endpoint reuses the network ADR's authenticated delivery but needs the brain reachable from the worker; the brain pushing (`PUT /blobs`) works when only the worker is reachable. Likely both, chosen per worker.
2. **Does a worker benefit from a GPU?** Not today: the thumbnails use the Workbench engine on the CPU, and the builds do not render. The benefit is cores, memory and isolation. A future Eevee or Cycles preview would change that.
3. **Concurrency.** The build lock serializes builds because they share the scratch directory and the manifest; a worker could run builds in separate scratch directories, but publish stays serial brain-side. Worth it only if rebuilds from the history (ADR_LOOK_HISTORY) become frequent.
4. **Model licences on someone else's machine.** The credits sidecar stays with the model on the brain. A licence such as CC-BY-ND (no sharing of modified versions) is about sharing; running the user's own conversion on the user's own worker is not sharing, but a worker shared between people would be. The docs should say workers are per user.
5. **Cache and privacy.** A worker keeps the user's models until evicted. A `DELETE /cache` and a configured maximum size are the minimum; whether to encrypt the cache at rest depends on who else uses the machine.
