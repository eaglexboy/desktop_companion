# Architecture Decision Record: Model-Written Body Scripts (Generated Bodies)

* **Status:** Proposed. Deferred to V2+; V1 ships only the groundwork listed in section 4.
* **Date:** 2026-10-27
* **Deciders:** Architecture & Core Engineering Team
* **Related:** [DESIGN_AVATAR_EVOLUTION_FLOW.md](DESIGN_AVATAR_EVOLUTION_FLOW.md), [SPEC_BLENDER.md](SPEC_BLENDER.md), [ADR_SKILLS_AND_AGENT_ORCHESTRATION.md](ADR_SKILLS_AND_AGENT_ORCHESTRATION.md) (sandboxing policy)

---

## 1. Context

The appearance designer picks from closed catalogs (`templates_catalog.json`, `wardrobe_catalog.json`). A user can ask for something none of them contain ("become a jellyfish", "a pirate coat") and the local model, shown only the catalogs, will sometimes name an option that does not exist. Today that delta fails catalog validation twice and the change is abandoned; the user is left with a body that is merely recoloured, or nothing.

The desired behaviour: when the catalogs cannot express what the companion wants, it asks the model to **write the Blender code for the body**, builds a `.glb` from it, and keeps that body as a reusable template.

This is a different class of feature from the rest of the appearance pipeline, because it executes model-written code:

* It needs a sandbox, a static check, a retry loop and a storage format, none of which V1 has.
* The code runs on the user's desktop with the user's privileges. A prompt-injected or merely wrong script is a real risk, not a theoretical one.
* Small local models write unreliable `bpy`; a useful loop needs build errors fed back and several attempts, which makes a change take noticeably longer than today's ~30 seconds.

So it is deferred, and V1 only adds the hooks that make adding it later a contained change.

## 2. Decision

1. **The model writes the body only, never the whole scene.** The script defines `build(ctx)` and returns the body mesh, where the head should go, and whether it has arms/legs. The existing generator still adds the head, eyes, mouth (all nine visemes), rig, animation clips, wardrobe and accessories. A generated body therefore cannot break lip-sync, the clip catalog or the manifest contract, and the existing ground-truth verification (`_verify_build_output`) applies unchanged.
2. **A restricted geometry API, not `bpy`.** The script is handed `prim(kind, ...)`, `material(name, hex)`, `assemble(name, parts)`, `math`, `mathutils`, `random`, and `ctx` (palette, proportions, target height 2.5 m, feet on `z = 0`, facing `-Y`). It gets no `bpy`, no imports and no file or OS access.
3. **Defence in depth, three layers:**
    * **Static check** (`blender/script_check.py`, pure stdlib so both the brain and the generator can import it): an AST allowlist rejecting imports, classes, `while`, `try`/`with`, dunder names/attributes, `exec`/`eval`/`open`/`getattr` and similar, with size caps. Run before a script is saved, and again inside the generator before it runs. Rejections are worded for the model to act on.
    * **Restricted execution:** the script runs with a minimal `__builtins__` and only the provided names.
    * **Bubblewrap around the whole Blender process** (the standard in the skills ADR; fail fast if `bwrap` is missing, never fall back to running unsandboxed): no network, no PID/IPC sharing, read-only system, a tmpfs `HOME`, the build directory the only writable path, and the user's home and `state.db` not mounted. The check is a seatbelt; the sandbox is the actual containment.
4. **A bounded repair loop.** Up to three attempts; each failure (static rejection or Blender build error) is fed back with the previous script. Local-model only; no cloud escalation by default.
5. **Generated bodies are stored as templates.** The accepted script is saved under `<deploy_dir>/assets/generated_templates/<slug>-<hash8>.py` and referenced from a `LookSpec` as `base_template = "generated:<slug>-<hash8>"`. Reverting is unaffected (it rewrites the manifest pointer to an existing `.glb`), and the designer is offered previously generated bodies as options alongside the catalog.
6. **User-prompted only at first.** Autonomous (reflection-driven) redesigns never trigger generation: it costs minutes of local-model time and nobody asked for it.
7. **Off by default, behind one switch:** `[appearance] allow_generated_bodies = false`.

## 3. Design

### 3.1 Trigger

Generation is entered from Trigger A (user-prompted) in three cases, in order of preference:

1. The designer includes `"custom_body": "<what it should look like>"` in its delta, an explicit escape hatch added to the delta prompt.
2. The designer names a `base_template` that is in no catalog (a hallucinated option). The invented name plus the user's request becomes the description.
3. Delta synthesis fails catalog validation on both attempts. The user's request becomes the description.

### 3.2 Flow

```
designing -> coding (attempt n/3) -> building -> done | failed
```

`coding` is a new stage of the `appearance_progress` event; the overlay's status pill shows `step/steps` taken from the payload (3 steps on this path, 2 otherwise).

1. Prompt the local model with the script contract, the palette and proportions, and the user's description. Extract the code (fenced or bare).
2. `check_script()`. On rejection, retry with the message.
3. Write the script to `generated_templates/`, set `base_template = "generated:<id>"`, and call `compile_and_publish()`, which wraps the Blender subprocess in `bwrap` for `generated:` templates.
4. On `GenerationError`, delete the candidate script and retry with the build error and previous code. After the third failure, report `failed` with the last error.
5. On success, keep the script, stamp redesign metadata and add a memory note.

### 3.3 Generator changes

`resolve_template()` gains a `generated:` branch (tier 4). `main()` builds the body from the script, then normalises it (feet on the ground, centred, `head_loc` shifted with it) and continues through the existing head/face/rig/clips path. A script that raises fails the build (so the repair loop sees the error) instead of falling back silently.

### 3.4 Open questions

* Is `bwrap` available inside every supported install (it is on the reference machine), and does the Blender binary location need extra read-only binds when it is not under `/usr`?
* How should the build-time budget interact with the 120 s `BUILD_TIMEOUT_S` when the model is slow, given that LLM time is outside that timeout?
* Should a generated body be offered for sharing/export, and if so, how is a script from elsewhere re-vetted?

## 4. What V1 ships (groundwork only)

* **The designer states what it could not do.** The delta prompt now says to pick the closest available options and add `"unmet": "<what is missing>"` when a request cannot be met. That is logged as an `appearance.unmet` event (status page timeline) and as a memory note, so the companion knows what it wanted and a later version has real requests to design against.
* **The `generated:` prefix is reserved.** Catalog validation rejects it with a clear message instead of treating it as an unknown name.
* **`allow_generated_bodies` is documented as a reserved config key** in `config.example.toml` (commented out, not read by V1).
* **A clearer failure.** When no valid design can be made from the catalogs, the status pill says the companion does not have the parts for that yet, rather than a generic failure.

Nothing in V1 executes model-written code.
