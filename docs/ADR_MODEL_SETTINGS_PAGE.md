# Architecture Decision Record: Per-Model Settings Page

* **Status:** Proposed. **Not implemented.**
* **Date:** 2026-11-11
* **Deciders:** Architecture & Core Engineering Team
* **Related:** [ADR_BUILD_YOUR_LOOK.md](ADR_BUILD_YOUR_LOOK.md) (the Admin app, its writing rules and the 3D viewer this page reuses), [ADR_MODEL_IMPORT_PAGE.md](ADR_MODEL_IMPORT_PAGE.md) (the page that brings models in; this one tunes them), [ADR_ACCESSORY_MODELS.md](ADR_ACCESSORY_MODELS.md) (sidecars, animation libraries, pack profiles, roles; Quaternius is the supported source), [ADR_IMPORTED_LOOK_LOCK.md](ADR_IMPORTED_LOOK_LOCK.md), [ADR_REMOTE_MODEL_PROCESSING.md](ADR_REMOTE_MODEL_PROCESSING.md) (what a job reads), [ADR_MULTI_USER.md](ADR_MULTI_USER.md), [SPEC_BLENDER.md](SPEC_BLENDER.md), [SPEC_OVERLAY.md](SPEC_OVERLAY.md) (what each role does)

---

## 1. Context

Everything that makes one installed model different from another is decided in one of three places, none of them friendly:

* **Automatically, from the rig.** Which sockets a body can carry, which animation libraries fit it, and which clip fills each role (idle, talk, greet, sit, workout...) come from the model's bone names and from the pack profiles (ADR_ACCESSORY_MODELS 1, 14, 22). The user cannot say "this model should not do the dance", "use this clip as its idle", or "this robot has no neck".
* **In hand-written files** beside the model: `<model>.pose.json` (`roll_bones`, `face_yaw`, `emissive_gain`), `<model>.face.json` (the face landmarks), `<model>.credits.json`, the `<model>.hold` marker. Each is a correction to one model, edited in a text editor, and found only by reading the ADRs.
* **Not at all.** An accessory item has one placement for every body (its own sidecar: `offset`, `scale`, `rotation`), and the parametric items are placed from the body's bones with no way to correct them for one model.

The last gap is the one that shows. Building every accessory on installed models (a Quaternius human, a Mixamo-rigged knight, a mech, and three mascots) showed head items fixed generically (they are now centered on the head and rest on it) but, on the others, items that are placed correctly *by the rules* and wrongly *for the model*: a scarf across the face of a model whose "neck" bone sits in its screen, a belt across the middle of a clock that is its whole body, a shield disc standing off a hand that is a blade. The rules read bones; a model whose bones do not sit where a person's do cannot be fixed by better rules, only by being told.

The Build page ([ADR_BUILD_YOUR_LOOK.md](ADR_BUILD_YOUR_LOOK.md)) chooses a look from what a model offers. This page changes what a model offers.

## 2. Decision

1. **A Model settings page in the Admin app.** It is its own page, separate from the Models import page: importing is "bring a file in", settings is "how this model behaves". It is a third writing page after Build and Models, with the same guards (POST/PUT/DELETE only, the page's own Origin, the `X-Deskmate-Admin` header, a small JSON body, the entry in `WRITE_PREFIXES`; ADR_BUILD_YOUR_LOOK 2). The page lists the installed bodies (held ones included, marked) and edits one at a time.

2. **What it changes, in four sections.**
    * **Animations and roles.** For the chosen body: which libraries it may use; which clips are switched off for it; and, per role, which clips fill it (the first is the role's own, the rest are its alternatives, ADR_ACCESSORY_MODELS 14), or that the role is off for this body. A role not mentioned stays automatic. Only libraries that fit the body's rig, and clips that exist in them, can be chosen, so a body that no library fits (ADR_ACCESSORY_MODELS 15) cannot be given roles: the page says so and offers what that body has.
    * **Accessories.** Which sockets the body allows and, per socket, which items (all by default). And per item **placement for this body** (offset in metres in the socket's frame, scale, rotation), layered on the item's own sidecar. A model file placement can be dragged in the viewer; a parametric item is adjusted by numbers and judged in a built picture (decision 5).
    * **Pose and face.** The settings of today's `.pose.json` (`roll_bones`, `face_yaw`, `emissive_gain`), and whether the arms are relaxed and by how much (`relax_arms`, `arm_spread`, today family-wide in the rig map). The face landmarks (`<model>.face.json`) are shown and can be edited by the same form, but stay their own file: they are measured from the model, not chosen.
    * **Availability and credits.** Hold or release (the `.hold` marker and its reason), and the credit and licence record (`<model>.credits.json`), shown with what the licence asks (credit, non-commercial, no derivatives).

3. **One settings file per model.** The new settings live in `<model>.config.json` beside the model in the deploy directory, as the existing sidecars do, so they travel with `models export` and `import` (ADR_ACCESSORY_MODELS 19: the file is in the bundle's manifest with a hash), a worker is sent them with the job's files (ADR_REMOTE_MODEL_PROCESSING), and a hand edit still works. Format 1:

    ```json
    {
      "format": 1,
      "animations": {
        "libraries": { "<library>": false },
        "clips": { "<clip>": false },
        "roles": { "idle": ["<clip>", "<clip>"], "dance": null }
      },
      "accessories": {
        "sockets": { "socket_neck": false },
        "items": { "socket_head_top": ["crown", "antenna"] },
        "placement": { "socket_waist": { "belt": { "offset": [0, 0, 0.1], "scale": 0.6, "rotation": [0, 0, 0] } } }
      },
      "pose": { "face_yaw": 180, "roll_bones": { "<bone>": 90 }, "emissive_gain": 2.0, "relax_arms": false, "arm_spread": 8 }
    }
    ```

    The file holds only what the user changed, never a copy of the defaults, so a better rule or a new pack still reaches a model that has not overridden it. `<model>.pose.json` keeps working: the `pose` section of the config wins over it, and the page writes the config and leaves the old file alone. Hold, credits and face data stay in their own files (decision 2), which the page edits in place.

4. **Settings are layered; the engine stays ignorant of any particular model.** The order for a role is the engine's defaults, then the pack profiles (install-wide), then this file (one model, always last), as the user's profile layer already sits over the shipped one (ADR_ACCESSORY_MODELS 22). `plan_animation_library`, `effective_wardrobe_catalog` and the socket placement read the file when they decide, so the designer, `validate_against_catalogs`, the Build page and the generator all see the same body. A stale entry (a clip that a library no longer has, an item that was removed) is a warning in the build and is ignored: a settings file never fails a build, and the page shows such entries as "no longer available" with a way to remove them.

5. **See it before saving.** The page reuses the Build viewer (ADR_BUILD_YOUR_LOOK 4): the body, its outfit, and the clip list, now as a role editor. Choosing a clip for a role plays it live as the build would wear it; a role's alternatives can be played one after another. Changes are held in the page until **Save**. Accessory placement needs the accessory drawn: model-file items are loaded by the viewer and can be dragged, while parametric items exist only in Blender, so the page offers an **alignment sheet**: a throwaway build (as Preview in ADR_BUILD_YOUR_LOOK 5) of the body with one item in every socket it carries, using the unsaved placements, rendered to a picture. It is the way to see what is off on this model and to check the fix. A preview never publishes.

6. **Saving is validated and atomic; applying is the next build.** The brain validates a settings document (format, known roles, libraries that exist and fit, clips that exist, sockets and items that exist for this body, numbers in range, no path or script anywhere) and writes it to a temporary file that replaces the old one only when complete. A saved change affects the next build of a look that uses the model; it does not rebuild by itself. When the current look is on that model the page says so and offers **Rebuild the current look** (the same call Apply makes, a new version, ADR_LOOK_HISTORY), because the look the user is looking at keeps its old behaviour until then. Roles an overlay already plays are unaffected until the rebuild's manifest reaches it (`library_clips`).

7. **Per user, and not in the repository.** The files are in the user's deploy directory next to their models (ADR_MULTI_USER) and are never part of the repository, the specs or these ADRs. The page names no model: it lists what is installed. Quaternius content is the supported source; settings do not make another source supported: they refine a model the converters already handle, and a model with no rig the libraries fit has no animation section to edit (ADR_ACCESSORY_MODELS 15).

8. **What it does not change.** The pack profiles themselves (which clips are a role's candidates, how a pack is converted) stay install-wide and are edited with `models add-profile`; a per-model override sits over them. What the overlay *does* with a role (when it plays, how often) is the overlay's, not a setting here (SPEC_OVERLAY). Rig conversion, `rig-body` options and mesh rebinding stay on the command line.

## 3. Consequences

**Good**

* The first fix for a model that does not line up is a setting, not a code change or a hand-written file: a model with an odd rig can say where its neck is not, and keep the rest of the rules.
* One place to see and change what a model can do, with the viewer to judge it, and one file that travels with the model.
* The designer, the Build page, validation and the generator stay in step because they all read the same file.
* Roles become a choice: a model can be stopped from dancing, or given the idle the user likes.

**Costs and risks**

* A new file format to version and keep stable (`format`), and one more thing for the bundle, the worker and the file-listing code to know about. The worker's guard test (ADR_REMOTE_MODEL_PROCESSING) must include it.
* Every reader of "what can this body do" must go through the settings, or one of them will disagree with the rest. A test that builds with a settings file and compares the options endpoint, validation and the generator's plan guards it.
* Accessory placement by numbers is fiddly for parametric items, which cannot be dragged. The alignment sheet costs a build (about 20 seconds) per look at it.
* A saved change does not show on the overlay until a rebuild, and a user may forget; the page says so and offers the rebuild.
* The page can make a model worse (every role off). It is reversible (remove the entry), and the page shows what a body keeps when a role is off.

## 4. Alternatives considered

* **The companion's database.** Easy to query, but the models folder would no longer describe itself, a bundle would not carry the choices, and a worker would need a second channel. Rejected (the user chose files).
* **Hand-edited files only, with documentation.** What exists. The point of this page is that a model's behaviour should not need a text editor and the ADRs.
* **Folding this into the Models import page.** One page less, but importing and tuning are different moments with different risks (importing takes untrusted files and runs conversions; tuning changes a text file), and the viewer belongs to tuning. Rejected (the user chose its own page).
* **Better placement rules instead of overrides.** Worth doing where a rule is wrong for many models (head items were), and some of that is done; no rule can know that a particular mascot's waist bone is its face.
* **Settings inside the pack profiles.** A profile is about a pack, not a body; per-model needs would bend it. The layering in decision 4 keeps profiles install-wide and puts the override above them.
* **A visual 3D editor for parametric accessories.** They exist only inside Blender today. Making them viewer objects would duplicate the generator's builders in JavaScript; the alignment sheet gets the same answer from the real code.

## 5. Implementation plan (for later; each step ships on its own)

1. **The format and its reader.** `<model>.config.json` format 1: the loader, the validator (against installed libraries, clips, sockets, items) and the writer (atomic), in pure Python with tests; `pose` falls back to `.pose.json`.
2. **Roles.** `plan_animation_library` and `pick_clips` take the animation section (libraries off, clips off, role lists, role off); the manifest's `library_clips` shows the result. Tests per rule, and that a stale entry warns and builds.
3. **Accessories.** `effective_wardrobe_catalog` honours sockets and items; the generator applies per-model placement over the item's sidecar; head, chest and waist items on a model with an odd rig can be corrected. Tests that options, validation and the build agree.
4. **Pose.** The `pose` section replaces reading `.pose.json` first, adds `relax_arms` and `arm_spread`.
5. **Export, import and the worker.** The bundle's file list, `CREDITS.txt`, the worker's `required_files`.
6. **Brain endpoints.** Read a model's settings with what is available to choose (libraries, clips with their roles, sockets, items, current values), validate, save, remove an entry; the alignment sheet as a preview kind.
7. **Admin write routes.** The new prefix in `WRITE_PREFIXES`, the guards, tests that nothing else became writable.
8. **The page.** The model list, the four sections, the role editor with the viewer, placement with numbers and dragging, the alignment sheet, Save, Rebuild. Rem lengths only.
9. **Documentation.** SPEC_STATUS_PAGE, SPEC_BRAIN, SPEC_BLENDER (the file and the layering), ADR_ACCESSORY_MODELS (the file beside the other sidecars).

## 6. Open questions

1. **Per-clip speed and variants.** A role's `time_scale` is a profile setting; should a model be able to slow its own idle, or switch off a role's `.low` / `.high` / `.mirror` variants? Not in the first version.
2. **Choosing among a role's alternatives.** The overlay picks one at random. A weight per alternative, or an order, would let a user prefer one clip. Not in the first version.
3. **Placement for model-file accessories.** They already have sidecars; should the page write per-model placement for them in the config (as decided) or offer to improve the item's own sidecar when the fix is right for every body?
4. **Applying to several models.** A user fixing the same offset on every Quaternius body would want "copy to...". Not decided.
5. **Dragging in the viewer.** Model-file items only, and it needs the item's own transform and the socket's frame in the browser, which the generator computes in Blender. Whether the brain exposes the socket frames or the page shows numbers only first is open.
6. **Where the face landmarks are edited.** They are measured, and a bad measurement is better fixed by measuring again (`face-sidecar --force`). Whether the page offers that, or only the numbers, is not decided.
7. **Format changes.** `format` is checked, but there is no migration story yet for a format 2.
