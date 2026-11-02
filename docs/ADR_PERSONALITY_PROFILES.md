# Architecture Decision Record: Loadable Personality Profiles

* **Status:** Proposed.
* **Date:** 2026-11-02
* **Deciders:** Architecture & Core Engineering Team
* **Related:** [ADR_LOOK_HISTORY.md](ADR_LOOK_HISTORY.md) (publish, rebuild from `LookSpec`), [ADR_AUTONOMOUS_VOICE_SELECTION.md](ADR_AUTONOMOUS_VOICE_SELECTION.md) (`agent_state` voice keys, pinning), [ADR_GENERATED_BODIES.md](ADR_GENERATED_BODIES.md), [ADR_SKILLS_AND_AGENT_ORCHESTRATION.md](ADR_SKILLS_AND_AGENT_ORCHESTRATION.md) (invariant 11, read-only status page), [SPEC_BRAIN.md](SPEC_BRAIN.md), [SPEC_BLENDER.md](SPEC_BLENDER.md)

---

## 1. Context

The companion today is a single, continuously evolving character. Its parts live in different places and none of them knows it belongs to a "character":

| Part | Lives in | Owner |
| --- | --- | --- |
| Body | `manifest.json` (`LookSpec` per version), `.glb` files, and imported models in `assets/custom_templates/` | brain, via `change_appearance` / `revert_appearance` |
| Traits and mood | `traits`, `mood` tables in `state.db` | brain, via reflection |
| Name | `identity` table | brain |
| Memory | `memories` table in `state.db` | brain, via reflection |
| Voice | `[voice.tts.remote.*]` in `config.toml` (`voice_sample`, `params`), optionally `agent_state.current_voice_id` | user, by hand; the daemon never writes `config.toml` |

Giving the companion a different character ("Miss Minutes", a stern butler, a jellyfish) currently means editing `config.toml` for the voice, restarting the brain because the TTS pool is built once at startup, rebuilding or importing a body, and either accepting the old traits and memories or resetting them. There is no way to go back to the earlier character without redoing all of it, and what it had earned is lost.

The wanted behaviour is one sentence: **"Load personality XXXX" and that character is the companion**, body, temperament and voice together, and it is still itself the next time it is loaded.

A second pressure shapes the memory design. The user may want either *different characters* (Miss Minutes and a butler who should not share a past) or *different personas of one companion* (who should), and may later run several companions at once (section 2.8). The design must support both without two mechanisms.

## 2. Decision

A **personality profile** is a named bundle of the things that make a character. Exactly one profile is *active* at a time. Loading one swaps all of them; unloading or swapping saves the evolved state back to the profile.

### 2.1 What a profile contains

1. **Body.** A reference to the body plus the `LookSpec` it was last wearing: either a catalog/`generated:` template with its wardrobe and palette, or an imported model in `assets/custom_templates/`. The spec is stored in the profile, so loading never depends on what `manifest.json` currently holds.
2. **Traits and name.** The full trait vector (`traits` table contents) and the identity name.
3. **Voice.** A TTS reference: the voice sample (and optional transcript) plus engine `params` (for example a Pocket `seed`). The sample file is copied into the profile directory, so a profile is self-contained and survives edits to `voices_dir`.
4. **Private memory.** The notes this character holds about its own experiences and its relationship with the user, capped at 200 like the existing memory notes.
5. **Memory mode.** `separate` (default) or `shared` (section 2.2).

Not in a profile: **mood** (transient, half-life 30 min; reset to its baselines on load), **conversation history** (`interactions` stays one global thread, so the status page and its single-database reads are unchanged), and **facts about the user** (shared by every personality; next section).

Everything except the voice sample and the initial body is *evolving state*: it is saved back on unload (section 2.4), never frozen at creation.

### 2.2 Memory: two tiers and a mode

Memory notes split by whose they are:

* **User tier (shared by all personalities).** Facts about the user: name, projects, preferences, hardware. The existing `memories` table **is** this tier. Every note already in it stays there and is treated as a user fact, so no migration is needed. The user tells the companion something once and every character knows it.
* **Self tier (per personality).** In-jokes, opinions, shared history, how this character feels about the user. A new table `self_memories(id, ts, note)` (created with `CREATE TABLE IF NOT EXISTS`, so still no ALTER) holds the **live** notes of the active personality; the profile file holds its **saved** notes. Same live/saved pattern as traits.
* **Isolation.** A personality never reads another personality's self tier directly. The butler does not know what Miss Minutes said, and no cross-reading mechanism exists, because it breaks the illusion of separate characters and is the hardest to reason about.
* **Memory mode is the one switch for "characters vs. personas".** `memory = "separate"` (default): this personality sees only the user tier and its own self tier. `memory = "shared"`: it additionally sees the saved self tier of every *other* profile that is also `shared`. Visibility is mutual and read-only; each persona still writes only its own notes. A family of personas of one companion is therefore just profiles all set to `shared`.
* **Tagging.** Reflection classifies each proposed note as `user` or `self`. A wrong `self` costs the user one repeated sentence; a wrong `user` leaks a private memory across characters, so **any note the classifier is unsure of defaults to `self`**. The user can force a note into the shared tier ("remember this for everyone"), which writes it to `memories`.
* **Retrieval** is unchanged in method (last 20 by recency, boosted by keyword overlap) over the union of the visible tiers.

### 2.3 Storage

Profiles are files, not rows, so one can be copied, backed up or shared as a folder:

```
<deploy_dir>/personalities/<slug>/
    personality.json      # schema below
    voice/reference.wav   # copied in at create time
    voice/reference.txt   # optional transcript
    thumbnail.png         # copy of the last look picture, for the status page
```

```json
{
  "schema": 1,
  "id": "miss-minutes",
  "display_name": "Miss Minutes",
  "created_at": 1730500000.0,
  "saved_at": 1730500000.0,
  "identity_name": "Miss Minutes",
  "traits": { "formality": 40, "extraversion": 85, "humor": 70 },
  "memory": { "mode": "separate", "notes": [ { "ts": 1730500000.0, "note": "..." } ] },
  "body": {
    "look_spec": { "...": "full LookSpec" },
    "imported_template": "miss_minutes_rigged"
  },
  "voice": {
    "sample": "voice/reference.wav",
    "sample_text": "",
    "params": { "seed": 1 }
  }
}
```

Writes are atomic (temp file in the same directory, then `os.replace`), matching how `manifest.json` is committed. The directory lives under `deploy_dir`, so it is covered by the existing cold backup (`deploy/backup_state.sh`).

`state.db` gains one `agent_state` key, `active_personality` (a profile id, or unset), and the `self_memories` table above.

### 2.4 Load

Triggered by the companion tool `load_personality(name)` ("load personality Miss Minutes"). Loading is **transactional from the user's point of view**: it either completes entirely or the previous personality stays active and unchanged.

1. **Resolve and validate** the profile: JSON parses, the voice sample exists, the imported template (if any) exists, the `LookSpec` validates against the current catalogs. Any failure aborts here with a plain explanation, before anything changes.
2. **Snapshot the outgoing personality** (section 2.5). This happens first so nothing earned is lost even if the load then fails.
3. **Body.** Build the incoming look through the normal appearance pipeline and publish it as a **new manifest version** (same rule as ADR_LOOK_HISTORY section 2.5: version numbers only increase). If a kept `.glb` already matches the stored spec's `seed` and `look_spec`, the manifest pointer is rewritten instead and no build runs. The overlay's progress pill is shown for the build (about 30 s). This is the only step that can take long or fail for external reasons, so it runs *before* anything is committed. On failure: progress `failed`, previous personality remains active, nothing else touched.
4. **Commit, in one SQLite transaction:** replace the `traits` rows, set `identity.name`, reset `mood` to baselines, replace `self_memories` with the profile's notes, set `agent_state.active_personality`, and clear `current_voice_id` / `previous_voice_id` / `staged_voice_id` / `staged_version` (the previous character's pending evolution must not leak into this one).
5. **Voice.** Point the TTS pool at the profile's sample and params without a restart (section 2.6), then run the existing remote warm-up with the new reference so the first spoken sentence is not the 5-6 s cold encode.
6. **Greet** in the new character, delivered exactly like the presence greeting (store, broadcast `chat_response`, `play_gesture`, speak). The prompt for that turn is built after the commit, so the first words already use the new traits, name and memories.

Loading the already-active personality is a no-op that says so. A load requested while a build or another load is running is refused with "still changing", never queued: a queued swap would execute minutes later against an unrelated conversation.

### 2.5 Save (unload / swap)

The `traits`, `identity` and `self_memories` data are the **live** state of the active personality; the profile file is its **last saved** state. Saving copies live to file.

* **On swap:** step 2 above.
* **On clean shutdown:** the same snapshot.
* **Periodically:** after each reflection cycle that changed traits or added notes, and after a look change, so a crash loses at most one cycle instead of everything since the load.
* **Explicit:** `save_personality()` snapshots now; `create_personality(name)` snapshots the current companion as a new profile ("save yourself as 'Jarvis'"), copying the current look's spec, the current traits, the voice currently resolved from `config.toml`/`agent_state` (reference file copied in), and, because it is that character's own history so far, the current `self_memories`. When no personality has ever been active the live self tier is empty and all existing notes are in the user tier, so the first profile starts with a clean self tier.

What is saved is what the character *became*: current traits, name, notes, and the look it is wearing now (so a character that redesigned itself returns in its latest outfit). Because the file is only replaced after the in-memory snapshot is built, a failed write leaves the previous saved state intact.

Nothing writes `config.toml`. The user's `[voice.tts.remote.*]` entries stay the default voice for "no personality active".

### 2.6 Per-personality voice without touching config

`voice_sample` and `params` are read from the immutable `TTSRemoteEntry` today. The decision is to add a **runtime voice override** on the TTS pool: while a personality is active, `synthesize()` receives its sample and params and applies them over the entry's own (`params` merged over, `voice_sample` replacing), for every remote entry whose provider supports a reference (`tts_serve`). Consequences:

* No brain restart to change voice; the pool is built once and keeps its engines and priorities.
* Engines that cannot clone (`generic_openai`, or VoxCPM1.5 which only takes seed pools) ignore the sample and speak with their configured voice. That is the *fallback* voice, as today. The personality's seed is only sent to engines that accept one.
* Piper fallback is unchanged and is not personality-specific.
* With `active_personality` unset, nothing is overridden and behaviour is exactly today's.

`voice_pinned` and autonomous voice selection (the V2 ADR) are **suspended while a personality with a voice is active**: the voice belongs to the character, and an autonomous swap would silently change who it is. A personality without a voice section keeps the V2 behaviour.

### 2.7 Interface

* **Companion tools:** `load_personality(name)`, `list_personalities()`, `save_personality()`, `create_personality(name)`. Matching is by id or display name, case-insensitive; an unknown name answers with the list rather than guessing.
* **Status page:** a read-only **Personalities** tab (thumbnail, name, traits, voice engine, memory mode, *active* badge). No load button: the page stays read-only (ADR_SKILLS_AND_AGENT_ORCHESTRATION, invariant 11) and the user loads by telling the companion. Backed by `GET /api/personalities`.
* **`GET /health`** reports `active_personality`.
* **Events:** `personality.loading`, `personality.loaded`, `personality.saved`, `personality.load_failed` on the existing event bus, so the timeline shows swaps.

### 2.8 Not decided here: several companions at once

The user may later run more than one companion concurrently (more hardware), for example in a group chat to get several perspectives on one problem. That needs several LLM slots, several voices, several overlay avatars and a turn-taking policy, none of which exists, so it is **out of scope**. This ADR only avoids closing the door:

* Profiles are self-contained folders addressed by id; nothing in the file format assumes "the" companion.
* Live state (`traits`, `self_memories`, the voice override, the active look) is the part that is singular today. Concurrent companions would turn that into per-profile live state keyed by id, with the profile file still the saved copy.
* The memory design is what makes a group useful: all participants share the user tier (the same facts about the problem and the user), each keeps its own self tier (its own view), and `shared` mode lets deliberate personas of one companion see each other.
* The tool surface already names profiles (`load_personality(name)`), so a later `join_personality(name)` is additive.

A separate ADR should cover scheduling (the Inference Capacity Governor already limits parallel LLM slots), audio mixing of simultaneous voices, and avatar layout.

### 2.9 Migration

Nothing changes until the user creates a profile. With `active_personality` unset the system is exactly V1. The first `create_personality` from the running companion is how Miss Minutes (template `miss_minutes_rigged`, `miss_minutes_dry.wav`, seed 1) becomes the first profile; that is the intended first use and doubles as the acceptance test, including the user-tier and self-tier split.

## 3. Consequences

* A character is a folder: easy to back up, copy between machines, or delete. Deleting a profile never touches `state.db` or the manifest, and deletes only that character's private memories; the user tier is unaffected.
* Swapping costs one body build (about 30 s with the progress pill) unless a kept `.glb` matches. The manifest history records each swap as ordinary look versions, and the Looks tab shows them, which is accurate: the companion did change.
* Reflection and autonomous redesign keep working unchanged on the live state; they are simply attributed to the active profile at the next snapshot. Reflection gains one classification (`user` vs `self`) per note.
* The TTS pool gains one mutable piece of state (the override). It is set only inside the load commit and cleared on unload, so it cannot disagree with `agent_state.active_personality` for longer than step 4 to step 5.
* A profile saved on a machine without the engine that produced its voice still loads: it falls back to whatever voice the remaining engines provide, with a warning on load.
* Profiles hold a copy of the voice sample, so a profile built from copyrighted audio is as sensitive as the sample itself. Profiles are under `deploy_dir`, which is not in the git repo; the repo must never contain one.
* Memory tagging errors are possible and biased toward privacy: a missed user fact must be repeated; a private memory is not exposed to other characters.

## 4. Alternatives considered

* **Profiles as `config.toml` sections.** Matches the voice catalog stubs, but the daemon must never write `config.toml`, so evolved traits could not be saved back, and a config reload would be needed per swap.
* **Profiles as rows in `state.db`.** Transactional, but it puts large blobs (voice WAV, `LookSpec`) and a second copy of every trait into the one database that SQLite already finds hard enough (WAL, NFS), and a profile cannot be copied or shared as a unit.
* **Separate `state.db` per personality.** Perfect isolation and a trivially atomic swap of everything, but it multiplies the WAL/backup surface, breaks the status page's single-database reads, and forces the user to re-teach every character the same facts about them.
* **Fully shared memory (everyone sees everything).** Simplest, but it makes characters indistinguishable in what they know, which defeats having a different character.
* **Fully separate memory (nothing shared).** Strongest isolation, but the user must repeat their own name, projects and preferences to each character.
* **Cross-readable private memories ("ask the butler what Miss Minutes said").** Rejected: it breaks character separation and is the most complex to build and explain. The `shared` mode covers the legitimate case (personas of one companion) without it.
* **Load only a subset (body-only, voice-only).** Useful, but already possible: `change_appearance` and the voice config. A profile's value is that the parts are loaded together; partial loads can be added later as arguments.
* **Queue swaps requested during a build.** Rejected: the user's conversation moves on during a 30 s build, and a swap that fires afterwards is surprising.

## 5. Open questions

1. **Tagging reliability.** How often does the local model misclassify `user` vs `self`? Measure on real reflection output with the first profile before relying on it; if poor, fall back to tagging only by explicit user instruction ("remember this for everyone") and defaulting everything else to `self`.
2. **Voice engines that cannot clone.** A profile whose only voice is a sample, on a setup with no cloning engine, falls back silently. Should load refuse, or warn in the greeting?
3. **Sharing profiles.** The folder format makes export easy; whether to ship an import/export command (and what to strip, for example the voice sample and private memories) is out of scope here.
4. **Shared mode and one-way visibility.** Visibility is mutual (both profiles `shared`). A one-way "this persona may read that one" is not supported; revisit only if a real case appears.
