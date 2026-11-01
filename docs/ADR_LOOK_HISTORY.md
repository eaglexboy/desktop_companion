# Architecture Decision Record: Look History, Thumbnails & Rollback by Version

* **Status:** Accepted. Implemented.
* **Date:** 2026-10-31
* **Deciders:** Architecture & Core Engineering Team
* **Related:** [DESIGN_AVATAR_EVOLUTION_FLOW.md](DESIGN_AVATAR_EVOLUTION_FLOW.md) (publish, prune, revert), [SPEC_BLENDER.md](SPEC_BLENDER.md), [SPEC_STATUS_PAGE.md](SPEC_STATUS_PAGE.md), [SPEC_BRAIN.md](SPEC_BRAIN.md)

---

## 1. Context

Every redesign adds a version to `manifest.json`, and `keep_recent_versions = 3` prunes all but the newest three `.glb` files as soon as a build publishes. Two things follow:

* Only "the previous look" is reachable by a user ("go back to your old look"). Anything older exists in the manifest as a `LookSpec` and nowhere a person can see.
* A user who liked version 12 has no way to *name* it: nothing shows what version 12 looked like.

The retention policy itself is sound (a `.glb` is several MB; thirty-odd of them are not worth keeping), and the manifest already keeps every version's full `LookSpec`, built deterministically from its `seed`. What is missing is a way to **see** history and a way to **ask for** a specific point in it.

## 2. Decision

1. **One PNG per version, kept forever.** Every published version gets a picture at `<deploy_dir>/looks/look-v{N}.png`. Pictures are small (a few tens of KB) and are never pruned, so history stays browsable however few `.glb` files are kept. `keep_recent_versions` is unchanged.
2. **Rendered by Blender, not by the overlay.** `blender/thumbnail.py` imports the built `.glb` and renders it with the Workbench engine (no GPU, about 2 s), orthographic, framed from the model's own rest-pose bounds so a tall and a short avatar both fill the picture, on a transparent background. The brain, not the overlay, owns this: the overlay may not be running, and the picture must be the same whichever client is attached.
3. **Backfill by rebuilding from the stored spec.** A version whose `.glb` was pruned is rebuilt from its `look_spec` into scratch, rendered, and the rebuilt file discarded. It takes the same build lock as a real build and never touches the manifest or the live assets. This runs at brain start for any version missing a picture (idempotent), and after a staged look is revealed. Builds are deterministic, so the picture is what that version looked like.
4. **Best-effort.** A failed render is logged and leaves a missing picture; it never fails or delays a look change (the render runs after the build lock is released).
5. **Rollback by version number.** `revert_appearance` accepts a version in the request ("go back to version 12", "restore look #7"). If that version's `.glb` is still kept it is a manifest pointer rewrite, as before. If not, the look is **rebuilt from its `LookSpec` and published as a new version**, so version numbers only ever increase and "current" is always the newest manifest entry or an explicit pointer rewrite to a kept file. Without a number the tool still steps back exactly one version.
6. **The status page shows, the companion restores.** A read-only **Looks** tab lists every version with its picture, summary, date and a *current* badge. It has no restore button: the page stays read-only (see [ADR_SKILLS_AND_AGENT_ORCHESTRATION.md](ADR_SKILLS_AND_AGENT_ORCHESTRATION.md), invariant 11), and the user restores by telling the companion the version number.

## 3. Consequences

* The pictures are the only new persistent data; they are derived, so deleting `looks/` is harmless (the next brain start re-renders them).
* Restoring an old look costs one build (about 30 s with the progress pill) instead of being instant, but is always possible while the manifest exists. This is why retention does not need to grow.
* A rebuilt look is a new version with an identical `look_spec`, so history shows it twice. That is accurate: the companion did change back.
* If a catalog entry a stored spec references is later removed, the rebuild fails like any other build failure (progress `failed`, current look unchanged) and the version's picture, if it exists, remains.
* Picture and rebuild share `_build_lock`; the backfill of a long history at first start delays a user-requested build by at most one render.

## 4. Alternatives considered

* **Keep every `.glb`.** Simple restore, but unbounded storage for a feature used rarely.
* **Render pictures in the overlay.** Needs the overlay running and a screenshot path per client; the pictures would differ by renderer.
* **A restore button on the status page.** Rejected: it would make the one read-only surface a control surface, and the companion already owns the conversation about its own appearance.
* **Thumbnails only for new builds.** Leaves the existing history blank; backfill from specs makes the tab useful on day one.
