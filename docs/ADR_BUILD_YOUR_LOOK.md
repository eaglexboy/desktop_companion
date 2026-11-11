# Architecture Decision Record: Build Your Look (Admin app)

* **Status:** Proposed. **Not implemented.**
* **Date:** 2026-11-11
* **Deciders:** Architecture & Core Engineering Team
* **Related:** [SPEC_STATUS_PAGE.md](SPEC_STATUS_PAGE.md) (the page this extends), [ADR_LOOK_HISTORY.md](ADR_LOOK_HISTORY.md) (versions, pictures, rollback; its "no restore button" decision is narrowed here), [ADR_IMPORTED_LOOK_LOCK.md](ADR_IMPORTED_LOOK_LOCK.md), [ADR_ACCESSORY_MODELS.md](ADR_ACCESSORY_MODELS.md) (what a body can carry; Quaternius is the supported source), [SPEC_BLENDER.md](SPEC_BLENDER.md) (the `LookSpec` and the generator), [ADR_MULTI_USER.md](ADR_MULTI_USER.md) (one brain and one Admin app per OS user), [ADR_NETWORK_AND_MOBILE_STRATEGY.md](ADR_NETWORK_AND_MOBILE_STRATEGY.md) (Origin/Host guards, auth token), [ADR_REMOTE_MODEL_PROCESSING.md](ADR_REMOTE_MODEL_PROCESSING.md) (where builds may run), [ADR_MODEL_IMPORT_PAGE.md](ADR_MODEL_IMPORT_PAGE.md) (the Models page, the other writing page)

---

## 1. Context

The companion's look changes in two ways today: the companion's own reflection cycle, and the user asking in chat ("change your look", "go back to version 12"). In both, a language model turns words into a `LookSpec` delta. A user who knows exactly what they want, such as one outfit, one accessory or a particular blue, has to describe it and hope the designer picks it.

The pieces to choose the look directly already exist:

* `LookSpec` is a closed schema (`brain/appearance/spec.py`): base template, four proportions, a four-colour palette, surface material, outfit, footwear, eight accessory sockets, `seed` and `has_face`.
* `effective_wardrobe_catalog(deploy_dir, base_template)` computes what a given body can wear, and `validate_against_catalogs` rejects the rest without running Blender.
* The brain already accepts a full spec: `POST /appearance/generate` (202, fire-and-forget, progress events) and `POST /appearance/revert`.
* The Looks tab already shows every version with its picture.

What is missing is a place in the UI to build a spec by hand and see roughly what it will look like first.

Two constraints shape the design:

1. **The status page is read-only on purpose** ([SPEC_STATUS_PAGE.md](SPEC_STATUS_PAGE.md), "Principles"; [ADR_LOOK_HISTORY.md](ADR_LOOK_HISTORY.md) decision 6 rejected a restore button for that reason). Building a look is a write, and a build takes about 20 seconds, so it is an action that needs protection from a stray or forged request.
2. **What a body accepts depends on the body.** A procedural body takes proportions, generic outfits and footwear. An installed body takes only the outfit models made for its skeleton and the sockets its rig can carry. A palette change over a textured model does little (ADR_ACCESSORY_MODELS, decisions 4 and 7). The page cannot show one fixed form.

## 2. Decision

1. **The status page becomes the Admin app.** It is a set of pages the user navigates between. The existing tabs (Overview, Thinking, Skills, Mood, Animations, Looks, Chat & memory, Logs) stay as they are, and **Build** is a new page beside them. The change is to the page title, navigation and documentation only. The module (`status_page/`), the service (`deskmate-status.service`), the `[status]` config section and the URLs keep their names so installed units and configs do not break. A full rename can be decided separately.

2. **Only a named set of pages writes: Build, and Models.** Every other page keeps the read-only guarantee: no write to `state.db`, no service control, nothing executed that the browser names. The guarantee in SPEC_STATUS_PAGE becomes "read-only except the routes of the pages named as writing", which are Build (this ADR) and Models ([ADR_MODEL_IMPORT_PAGE.md](ADR_MODEL_IMPORT_PAGE.md)). A new writing page needs its own decision and an entry in that list. ADR_LOOK_HISTORY decision 6 is narrowed the same way: the Looks tab still has no restore control, and Build is where the user acts. The write routes are a separate, named set in the server, each of them:
    * accepts only `POST`, with a JSON body, and refuses a request whose `Origin` is present and not the page's own (the existing `Host` guard stays);
    * requires the bearer token when `server.auth_token_file` is configured (ADR_MULTI_USER 2.2);
    * names things only by value from the catalogs (a spec, never a path, a command or a script);
    * is passed to the brain, which validates it again. The Admin app holds no appearance logic and never touches the manifest or the deploy directory's assets.

3. **What the user can choose.** The form is built from the body's own options, read from the brain, not hard-coded:
    * **Body:** every installed body that is not held back, Quaternius bodies first, as the supported source (ADR_ACCESSORY_MODELS 15). A body from another source is listed with the features its converter gives it (the same facts as `models list`).
    * **Outfit and footwear:** the generic garments on a procedural body, and the outfit models made for the chosen body. An installed body is offered no footwear and no generic garments, as the catalog already says.
    * **Palette and material:** the four colours and the surface material. On a textured model the page says that colour does little instead of hiding the control.
    * **Accessories and proportions:** one picker per socket the body's rig can carry, and the four proportion sliders. Proportions apply to procedural bodies only.
    * A control that does not apply to the chosen body is shown disabled with the reason, so the user learns what a body can do and does not wonder where an option went.
    * **Animations are viewed, not chosen.** The 3D viewer (decision 4) plays any clip the chosen body supports, so the user sees how the body will move. Which clip fills which role stays automatic (ADR_ACCESSORY_MODELS 14, 22) and is not part of the `LookSpec`, so Build does not change it. Choosing clips per role, or pinning a library, is a possible later step (open question 6).
    * Seed and `has_face` are not exposed; the seed is kept from the current look unless the user asks for a variation (open question 2).

4. **A live 3D viewer for body, outfit and animations.** The page shows the chosen body in a three.js viewer (orbit camera, play and pause, a clip list) so the user sees it move without waiting for a build. It is modelled on how the [Mesh2Motion web app](https://github.com/Mesh2Motion/mesh2motion-app) (MIT code; its art is CC0) loads a rigged model and plays library clips: `GLTFLoader`, then an `AnimationMixer` that binds each clip to the skinned meshes by bone name. This fits here because the Quaternius bodies and libraries already share bone names (ADR_ACCESSORY_MODELS 14, 15), so a clip needs no renaming. The viewer:
    * **Shows live:** the body, the outfit model made for it (it is skinned to the body's skeleton, ADR_ACCESSORY_MODELS 7), and every clip the body supports. The clip list is the one the Animations page already has: the libraries the body fits, the roles each clip fills, whether it sits on a chair or the ground, and the bones the body lacks. A clip's tracks for bones the body lacks are ignored, as `wear_animation_library` drops them in the build. A body that was converted (ADR_ACCESSORY_MODELS 18) plays its own retargeted libraries, not the shared ones.
    * **Plays clips as the build does.** The generator moves only the hips' travel, scaled by the ratio of the two hip heights (ADR_ACCESSORY_MODELS 14). The viewer does the same, so feet do not sink or float, and it uses the body's own rest pose without relaxing the arms, as a body that wears a library does.
    * **Does not show:** accessories, palette, surface material and proportions, which only a build applies. The viewer says so on the page, and Preview (decision 5) is how the user sees them exactly. The overlay draws with a different renderer (Qt Quick 3D), so lighting, glow and the face overlay (blinking, lip-sync) differ from the viewer. The viewer is a guide to shape and motion, not a copy of the overlay.
    * **Is served by the Admin app, read-only.** three.js and its `GLTFLoader` and `OrbitControls` are vendored under `status_page/static/` (the page has no build step and no external assets, SPEC_STATUS_PAGE), with their licence. Model and library files are served by a new read-only route that names a body, an outfit or a library by id from the catalogs and never takes a path, and only for models that are installed and not held back. These files are the user's own models in the deploy directory; they are served to the browser on the same machine and are not copied into the repository, the specs or the ADRs.
    * **Treats files as untrusted.** The browser parses a model with three.js, not with Blender. Only files already installed by `models add-*` are served, and the viewer's vendored code is fetched once into a scratch directory and read before it is committed.

5. **Previews of a build are on demand.** Dragging a control updates the swatches at once, but it builds nothing. A **Preview** button asks the brain for a throwaway build of the spec, rendered to a picture the same size as the Looks tab's. This is how accessories, palette, material and proportions are seen exactly. The preview:
    * runs in scratch and is never published: no manifest entry, no version number, no change to the live look or its files, as the history backfill already does (ADR_LOOK_HISTORY 3);
    * takes the same build lock as a real build, so a preview waits for a build under way and a build waits for a preview, and the page says it is queued;
    * takes about as long as a build (about 20 s), so the page shows progress and keeps the previous preview, marked as out of date, until the new one arrives;
    * is dropped when the page is closed or the user previews again, and any preview left behind is removed at brain start.
    Preview is the only way to see a spec's picture before it is a version; the page does not try to be instant.

6. **Apply makes a normal new version.** Apply publishes the spec as the next version through the existing generate path. History, the picture, `keep_recent_versions` and rollback by number all work as for any other change, and "go back to version N" in chat undoes it. Applying is the user acting directly, so it is not blocked by the imported-look lock (ADR_IMPORTED_LOOK_LOCK 6): **whatever look it makes current decides the lock.** A generated look built here is unlocked as soon as it is current. A look built on an imported model stays locked, so the companion still will not redesign it on its own; the page says so before Apply. Apply never changes `look_unlocked_for`.

7. **The page shows the look history beside the form.** The current version and its summary are shown above the form, and **Start from** offers the current look or any earlier version from the history (its stored spec fills the form). Undo is the Looks tab's version number; Build adds no separate undo.

8. **Per user, nothing extra.** The Admin app belongs to one OS user's brain and edits that user's companion, with models from that user's deploy directory (ADR_MULTI_USER). Build needs nothing beyond that ADR's rules, and the write routes follow its token and Origin rules once they exist. Until then, on a shared machine, the protection is the loopback bind and the Origin check, which is the same protection `/chat` has today. The page and specs do not name any user's models: what is offered comes from the deploy directory at runtime.

9. **Where builds run is not this ADR's concern.** Preview and Apply call the same build path the brain uses for every look. If a remote worker is configured (ADR_REMOTE_MODEL_PROCESSING), they use it; if not, they run locally.

## 3. Consequences

**Good**

* A user can get exactly the look they want without a conversation, and sees a picture before committing.
* One path to a new look: Apply is a version like any other, so history, the picture and rollback need no change.
* The form follows the body, so a new model works with the page the moment it is installed, with no UI change.
* The read-only guarantee holds for every page except the named writing pages (Build and Models), which are narrow and guarded the same way.

**Costs and risks**

* The Admin app can now cause 20-second builds and a change to the companion's visible look. The Origin check and token keep a webpage in another tab from doing it, but a local process can still call the brain's API directly, as it can today.
* A preview costs a full build. Previews queue behind builds, and a busy machine is slower; the page shows the queue.
* The viewer adds a 3D library to the Admin app (vendored, with its licence) and a new route that serves installed model files to the browser. The route is read-only and by id, and it is the new surface to test: no path in, no held model out, nothing outside the model folders.
* The viewer and the overlay are different renderers, so they can disagree on lighting, glow and the face. The viewer cannot be used to judge the overlay's look; it exists to show shape and motion, and the page says so.
* The viewer's hip scaling and bone handling must follow the generator's, or it would show a clip that the build does not play (feet sliding, a limb missing). A test compares the two for a body and a library.
* A new brain endpoint is needed to return a preview picture and the options for a body. It is read-only apart from the scratch build.
* The preview and the applied look use the same spec and seed, so they match on the same Blender version. On a different version they may differ a little (ADR_REMOTE_MODEL_PROCESSING, "Costs and risks").
* SPEC_STATUS_PAGE, ADR_LOOK_HISTORY and its tests state that the page is read-only. They must change together with the code, or the read-only tests will rightly fail.

## 4. Alternatives considered

* **A tab on the status page with no other change.** Findable and reuses the Looks cards, but it drops the read-only guarantee for the whole page and gives no reason for the new write routes to be different from the rest.
* **A separate Build service or page on its own port.** Keeps the status page pure, but it is a second thing to install, secure and bind, and it was not the shape the user asked for.
* **The browser talks to the brain directly.** The Admin process stays read-only, but the brain would need CORS and its own browser-facing auth. The Origin and token checks belong in one place, in front of the routes that write.
* **Live preview while dragging.** Not possible: a build is about 20 s. A fast approximation (a flat 2D swatch or a generic mannequin) would show something other than the look the user gets.
* **Animations as part of the spec now** (a clip per role, or a pinned library). It changes the spec, the generator and the manifest, and the overlay's behaviour for every version. The user chose a view-only viewer first; this can follow once the viewer shows what is available.
* **Only the built picture, no live viewer.** Simple, but every change to body, outfit or animation would cost 20 s, and the user could not see motion at all, which a picture cannot show.
* **Playing the clips in the overlay's renderer from the page** (a Qt window or a streamed view). The overlay is a native window, not a web page, so this would need a second client for the Admin app to embed.
* **Writing a viewer from scratch instead of using three.js.** glTF skinning and animation mixing are what three.js already does and what Mesh2Motion uses. Only its approach is borrowed, not its code; the viewer is written for this project's models.
* **A larger preview now** (ADR_ACCESSORY_MODELS open question: a bigger render, then a turntable). Wanted eventually and left for its own decision; this page uses the Looks tab's picture size.
* **Applying also unlocks an imported look.** It would let the companion redesign a model the user just hand-built. Rejected: the lock is derived from what is current and stays that way.
* **Refusing to build while locked.** Rejected: building by hand is the user acting directly, which the lock already allows (ADR_IMPORTED_LOOK_LOCK 6).

## 5. Implementation plan (for later; each step ships on its own)

1. **Options for a body.** A brain endpoint (read-only) returning, for a body, the controls that apply and the choices in each, from `effective_wardrobe_catalog` and the body's capabilities, and the installed bodies with what each can do. Tests per body kind: procedural, Quaternius, installed with and without outfit models.
2. **Viewer.** The read-only model route (body, outfit, library by id), the vendored three.js, and the viewer: load a body and an outfit, list and play the clips it supports, hip scaling as the generator does it. Tests for the route (no path, no held model, nothing outside the model folders) and for agreement with the generator on hips and missing bones. Quaternius body and library first; a converted body and its own libraries next.
3. **Preview.** A brain endpoint that builds a spec in scratch under the build lock and returns a picture; cancellation, clean-up at start, a limit of one at a time. Tests that nothing is published and the live look is untouched.
4. **Apply.** Reuse `POST /appearance/generate`; check that the lock behaves as in decision 6 for generated and imported results.
5. **Admin write routes.** The Origin and token checks, the named set of write routes, and tests that every other route refuses `POST` and that the read-only tests still hold.
6. **Admin app and Build page.** Navigation and title (UI and docs only), the form from the options endpoint, swatches, Preview and Apply, Start from, progress. CSS lengths in `rem` and no sizes in JavaScript, as the Sizing rules require.
7. **Documentation.** SPEC_STATUS_PAGE, ADR_LOOK_HISTORY decision 6, SPEC_BRAIN (the endpoints), README.

## 6. Open questions

1. **Preview picture on a remote worker.** The picture is made where the build runs; it reaches the page through the brain, not from the worker. Whether the brain keeps it in memory or in a scratch file depends on size.
2. **Variations.** Should the page offer a "different seed" button for a procedural body, since the seed changes small details? It is not in the first version.
3. **Reuse of a preview.** If the user previews a spec and then applies the same spec, the build could reuse the preview's `.glb`. It saves 20 s but couples the two paths (the file must be proven to be the one the spec describes). It is not in the first version.
4. **Named presets.** Saving a spec under a name to return to later overlaps with the version history. Not decided.
5. **Who else may call the write routes** before ADR_MULTI_USER's token auth exists. The Origin check is the only guard until then.
6. **Choosing animations.** The viewer shows what a body can play, but the look does not record a choice. A later step could add a per-role clip or a pinned library to the spec. It needs a generator change and a manifest record, and the overlay would have to honour it.
7. **A viewer for the built look.** The viewer shows the installed body and outfit, not the built `.glb`. Playing the built file (with its accessories and palette) would make the viewer match the build, but only after a build, and could replace the picture in Preview. It also depends on the larger-preview and turntable question in ADR_ACCESSORY_MODELS.
8. **Bodies the viewer cannot show well.** A body from a source with no converter plays no libraries (ADR_ACCESSORY_MODELS 15), and a model with no mouth shape keys has its face drawn by the overlay, which the viewer does not draw. The page says what it is not showing.
