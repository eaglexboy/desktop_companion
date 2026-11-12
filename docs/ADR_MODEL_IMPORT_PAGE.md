# Architecture Decision Record: Importing Models from the Admin App

* **Status:** Proposed. **Not implemented.**
* **Date:** 2026-11-11
* **Deciders:** Architecture & Core Engineering Team
* **Related:** [ADR_BUILD_YOUR_LOOK.md](ADR_BUILD_YOUR_LOOK.md) (the Admin app and its write rules; this page is a second writing page), [ADR_ACCESSORY_MODELS.md](ADR_ACCESSORY_MODELS.md) (the install commands, conversion, bundles, licences; Quaternius is the supported source), [ADR_IMPORTED_LOOK_LOCK.md](ADR_IMPORTED_LOOK_LOCK.md), [ADR_REMOTE_MODEL_PROCESSING.md](ADR_REMOTE_MODEL_PROCESSING.md) (where conversions may run), [ADR_MULTI_USER.md](ADR_MULTI_USER.md), [SPEC_STATUS_PAGE.md](SPEC_STATUS_PAGE.md)

---

## 1. Context

Models are installed today with `python -m brain.appearance.models` (`add-body`, `rig-body`, `add-outfit`, `add-accessory`, `add-animations`, `import`). It works, but it needs a terminal, knowledge of the options, and a decision about every file's name and kind. A user who has downloaded a character, an outfit or a clip pack cannot see what the install will do with it before it has done it, or what the licence asks, until it prints on the console.

The Admin app (ADR_BUILD_YOUR_LOOK) is where the user already chooses the companion's look. A page to bring in a new body, outfit, accessory or animation library lets the user add a model and then use it, in Build, or leave it for the companion to choose on its own.

Importing is riskier than building a look:

* **The input is untrusted.** A download is parsed by Blender during conversion, and by the install code before that. A hostile or damaged file, a zip that unpacks to a path outside its folder, or a very large file are all realistic.
* **It writes the user's assets**, which the live companion reads. A half-finished install, or one that runs while a look is being built, can leave the deploy directory inconsistent.
* **It takes minutes** (a large Sketchfab download measured well over a minute in conversion, and libraries are retargeted onto every converted body).
* **Licences matter.** A model may need credit, be for non-commercial use, or forbid modified copies. The install prints this; nobody sees it when the companion simply starts wearing the model.

## 2. Decision

1. **A Models page in the Admin app.** It is a second page that writes, beside Build. The rule from ADR_BUILD_YOUR_LOOK still holds: every other page is read-only, and the pages that write are a named set. Models joins that set, with the same checks (POST only, the page's own Origin, the bearer token when `server.auth_token_file` is configured, no paths or commands from the browser). SPEC_STATUS_PAGE's list of writing pages gains this one.

2. **What it imports, and what is supported.** Four kinds, each one the existing install code with a form in front:
    * **Bodies:** a `.glb`, `.gltf` with its files, or `.fbx`. The page runs the same detection the install does (Quaternius, Mixamo, Sketchfab, Character Creator, Auto-Rig Pro, a rig map, a skeleton-less model of separate parts) and says which it found.
    * **Outfits and accessories:** an outfit for a chosen body, or an accessory for a chosen socket.
    * **Animation libraries:** a clip pack (`.glb`, `.fbx`), with an optional pack profile (`.json`), validated before it is installed (`check-profile`).
    * **Bundles:** a `models export` zip from another install, verified by the bundle import before anything is written (`--dry-run` is the page's first step).
    **Quaternius content is the supported source** (ADR_ACCESSORY_MODELS 15): it installs with every feature. Content from another source is only as supported as its converter. The page shows, for each file, one of: *supported* (Quaternius), *converted* (a known rig family or a rig map: it gets the libraries through retargeting), or *generic only* (installed as a `custom:` body that gets the rig-reading features and no outfits or libraries). The user sees this before installing.
    **Not on the page in the first version:** `rig-body` options (facing, arm offset), `rebind-mesh`, and hand-written sidecars (`.pose.json`, `.face.json`). They are corrections to one model and stay on the command line.

3. **Two ways in, one quarantine.** A file arrives by **upload** (the browser sends it) or by **picking a file from a folder on this machine** (`[models] import_dir`, e.g. a downloads folder, unset by default). Either way it is copied into a quarantine folder, `<deploy_dir>/imports/<id>/`, and all work is done on that copy, never on the original:
    * **Upload** is streamed to disk, not held in memory, with a size limit (`[models] max_upload_mb`, default sized for the largest download the converters handle) and a free-space check first. The browser's file name is shown but never used as a path; the stored name is generated.
    * **Folder pick** lists only the files directly in `import_dir`, by name and size. The browser names a listed entry by id; it cannot supply a path. A symbolic link, a directory or anything outside `import_dir` is not listed or accepted.
    * **A file's kind is read from its content** (a `.glb` starts with `glTF`, a zip with its signature, an `.fbx` with its header), not from its extension alone, and a mismatch is refused with the reason.
    * **A zip** (a bundle, or a glTF with its files) is never unpacked by the page itself. Bundles go through the bundle import's checks (every name a relative path under a model folder, sizes and hashes verified, a total size cap, staged before anything is moved, ADR_ACCESSORY_MODELS 19). A glTF zip is unpacked into its own empty directory with the same name checks.
    * Quarantine folders are removed when the import ends, fails or is cancelled, and any left from an earlier run are removed at brain start. Files in quarantine are read with Python's isolated mode when the page inspects them, as the project does for all downloads.

4. **The brain runs the import; the Admin app only asks.** The Admin app stays a thin front: it forwards the request, relays progress and holds no model logic. The brain runs the install, because the install must not run beside a look being built:
    * An import takes **the same lock as a build**, and one import runs at a time; a second is queued and the page says so.
    * It goes through the conversion path the brain already uses for model work, so when a remote worker is configured (ADR_REMOTE_MODEL_PROCESSING, job kinds `convert`, `inspect`, `retarget`) a hostile file is parsed on the worker and not on the user's desktop. Without a worker it runs locally, as the command line does.
    * Progress (inspecting, converting, inspecting the rig, retargeting libraries, writing) is streamed to the page, and the import can be cancelled; a cancelled or failed import leaves the deploy directory as it was (the install writes into place only once the conversion has succeeded, as `--replace` already does).

5. **A flow of three steps, with nothing written until the second.**
    1. **Inspect.** The page detects the kind and support level, and shows what the install would do and the licence and credit information the file carries (author, title, source, licence, and what the licence asks: credit, non-commercial, no derivatives). For a bundle it shows the `--dry-run` result: what is new, what conflicts.
    2. **Install.** The user confirms the kind and the options the install needs: for a body, its name (`custom:<name>`, letters, digits and underscores) or `--replace` if the name is taken; for an outfit, the body it is made for and its name; for an accessory, its socket and name; for a library, its name and an optional profile. A name that is already installed is refused unless the user chooses to replace it, and replacing a body that the current look wears is refused as `remove-body` refuses it.
    3. **Result.** The page shows the report the install makes (the rig, the sockets the body can carry, what each library gives it, warnings, a face sidecar result) and the credits.

6. **Every import starts held back.** A new body, outfit, accessory or library is installed with a hold, with the reason "imported, not released", using the existing `.hold` mechanism (ADR_ACCESSORY_MODELS 11). A held model stays on disk and is shown on the page with its credits and what its licence asks, but it is not offered to the companion's designer, not accepted for a look, and not offered in Build, until the user chooses **Release**. This puts the licence decision with the user and stops the companion choosing a model on its own redesign before the user has seen it. Release is a button on the page that only the user can press; the companion has no skill that releases a model. A bundle import holds what it adds in the same way. After release, a body is offered like any other `custom:` body: to the companion for its own redesigns (unless it is wearing an imported look, ADR_IMPORTED_LOOK_LOCK) and to the user in Build.

7. **Managing what is installed.** The page lists installed bodies, outfits, accessories and libraries, with the same facts as `models list` (what each body can carry, which libraries fit it, held or released, credits). From it the user can **Release** or **Hold** a model, and **Remove** one. Remove shows what will go (the model, its sidecars, the libraries retargeted onto it) before it deletes, and refuses the body of the look the companion wears now. It never deletes anything it did not list.

8. **Per user, models stay out of the repository.** Everything imported lives in that user's deploy directory (ADR_MULTI_USER). Imported models, packs, their credits and any pack profile are the user's own content: they are never written into the repository, the specs or the ADRs, and the page does not ship any.

## 3. Consequences

**Good**

* A user can bring in a model, see how well it is supported and what its licence asks, and use it, without a terminal.
* The licence is seen before use, and the companion cannot start wearing a model the user has not released.
* Untrusted files are copied, size-limited, typed by content and, with a worker, parsed away from the desktop.
* The install logic is not duplicated: the page is a front end for the commands that exist and are tested.

**Costs and risks**

* The Admin app gains a second writing page and a file upload endpoint. The upload is the largest new surface: size, type, free space and the quarantine all need tests, including an oversized upload, a file with the wrong content for its type, a zip with a path that escapes, and an entry that is a symbolic link.
* Imports hold the build lock for the length of a conversion (minutes), so a look change waits. The page and the companion say so (the existing progress pill).
* Conversions need Blender. Without it (and without a worker) the page shows only what can be done without it (a bundle with its derived libraries, a `.glb` that needs no conversion) and says why the rest is unavailable.
* Held-by-default adds a step for every import. That is intended, but a user who imports ten files will click Release ten times. A "release all from this import" is in the page's first version for that reason.
* The CLI's `SystemExit` errors are written for a terminal. The brain-side import wrapper turns them into structured errors the page can show.

## 4. Alternatives considered

* **Run the install in the Admin process.** Simpler to wire, but the Admin app is meant to stay restartable and light, and it would not hold the build lock or reach a worker. Rejected.
* **Command line only.** Safe and exists. Rejected as the only path because the user asked for a page, but the commands remain the fallback and the place for expert options.
* **A watched folder: drop a file and it installs.** No page needed, but no inspection step, no choice of kind or name, no hold, and a file that appears half-copied. Rejected.
* **Available at once, held only on a warning licence.** Quicker, but the user said to hold until they release it, and a missing licence is common enough that the warning would often be absent.
* **Letting the companion import or release.** A model with a licence the user has not read would enter the companion's choices. Release is the user's decision.
* **Only upload, or only a folder pick.** Upload is the same on every platform; a folder pick avoids pushing hundreds of megabytes through the browser. Both were chosen.

## 5. Implementation plan (for later; each step ships on its own)

1. **Structured install results.** A brain-side wrapper over `add_body`, `add_outfit`, `add_accessory`, `add_animation_library` and the bundle import that returns a report and structured errors, takes the build lock and writes only on success. Tests that a failure leaves the deploy directory unchanged.
2. **Detection.** A read-only inspect call (kind, support level, credits, the dry-run for bundles) on a quarantined file, without Blender where possible.
3. **Quarantine and intake.** The upload endpoint (streaming, size limit, free space, content typing), the `import_dir` listing, clean-up at start. The security tests above.
4. **Hold by default and Release.** The hold with its reason on every import, and Release, Hold and Remove with the remove preview. Check the designer's catalog and `validate_against_catalogs` omit held models.
5. **Jobs.** Queue, progress events, cancellation, one at a time; use the worker pool when it exists.
6. **The Models page.** The three-step flow, the installed list, the support level and licence display. CSS lengths in `rem`, no sizes in JavaScript.
7. **Documentation.** SPEC_STATUS_PAGE (the writing pages), ADR_ACCESSORY_MODELS (the page as a front end to the commands), SPEC_BRAIN (endpoints and the `[models]` settings), README.

## 6. Open questions

1. **Size limit.** What default `max_upload_mb` fits the largest download the converters accept without making the upload endpoint a way to fill the disk? The free-space check bounds the damage; the default needs a measurement.
2. **A hostile file and no worker.** Without a worker, Blender parses the file on the desktop. A resource limit on the conversion subprocess (memory, time) would help; whether that is in scope here or in the worker ADR is not decided.
3. **Updating a model.** `--replace` removes the old model and its retargeted libraries once the new file is ready. The page needs wording that makes clear what is lost (a facing set by hand in a sidecar, for instance).
4. **Pack profiles.** Whether uploading a pack profile belongs with the library import (as here) or in its own control, since one profile can apply to packs installed at different times.
5. **Where the credits are shown to others.** A licence that asks for credit "wherever the model is shown or shared" may apply to the overlay. The credits are recorded and shown here; whether the overlay or a share surface must also show them is not decided.
