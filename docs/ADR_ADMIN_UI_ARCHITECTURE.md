# Architecture Decision Record: How the Admin App's Interface Is Built

* **Status:** Accepted. Decisions 1-3 and 6-8 are implemented for the Build page. Decision 4 (a page's markup out of `index.html`) is not done yet, and the older modules and the next two pages follow these decisions as they are written.
* **Date:** 2026-11-11
* **Deciders:** Architecture & Core Engineering Team
* **Related:** [SPEC_STATUS_PAGE.md](SPEC_STATUS_PAGE.md) (the app, its layout and its sizing rules), [ADR_BUILD_YOUR_LOOK.md](ADR_BUILD_YOUR_LOOK.md), [ADR_MODEL_IMPORT_PAGE.md](ADR_MODEL_IMPORT_PAGE.md), [ADR_MODEL_SETTINGS_PAGE.md](ADR_MODEL_SETTINGS_PAGE.md) (the two form-heavy pages still to come)

---

## 1. Context

The Admin app's interface is plain ES modules with no build step and nothing loaded from outside (SPEC_STATUS_PAGE). That suited a read-only dashboard. It is now a dashboard plus one page that writes (Build), with two more form-heavy pages proposed (Models, Model settings). The question is whether to change how the interface is built before those are written, because changing it later costs more.

### 1.1 What the code looks like now (measured 2026-11-11)

* **Size:** 17 JavaScript modules, 2,979 lines, 107 KB, plus 1,122 lines of CSS. `build.js` is 811 lines (formatted at 80 columns) and is the only module that is large; the next is 285.
* **Where the boilerplate is, counted in the code:**
    * 106 calls to `esc()` for escaping text put into HTML strings (27 in `build.js`).
    * 40 `innerHTML` assignments (4 in `build.js`) building markup from template strings.
    * 64 `$("id")` lookups in `build.js` alone.
    * 7 functions in `build.js` whose only job is to bring the page back in step with the state after a change (`updateColors`, `updatePreviewOnly`, `syncAccessories`, `renderActions`, `renderForm` and others), and a loading counter, a viewer job counter and a "settled" flag to stop stale results landing.
* **What is already good:** the logic that matters is pure and has its own tests (`build-spec.js`, run with node), the model viewer is its own module, state lives in one object, and each page has its own stylesheet.
* **Bugs this style has caused:** the form losing focus when it was re-drawn (so it is updated in place instead), a label that needed a separate sync to appear, a stale scratch brain that made the new code path silently not run, and a brain older than the page (the page now tolerates it). None was a size problem. Each was a state-to-screen synchronisation problem.

### 1.2 What is coming

The Models page (upload, a queue of imports with progress, an installed list with release, hold and remove) and the Model settings page (a role editor, accessory placement, a viewer, saved and unsaved changes) are forms with a lot of shared state, lists that change while the user works, and live progress. They are each likely to be as large as Build.

### 1.3 Constraints

* No build step and no external assets: a Python project, a loopback service, and third-party code vendored and verified (three.js already is; its integrity is checked and Prettier is told to leave it alone).
* Lengths in `rem`, no sizes in JavaScript, formatted with Prettier's defaults (`.prettierrc.json`).
* Page size is not a concern: the app is served from the same machine, so 107 KB uncompressed costs nothing measurable. "Smaller files" in this ADR means less code to write and read, not bytes on the wire.

## 2. What was tried

A proof of concept, kept out of the repository, redid two real pieces of `build.js` with **Preact and htm**: the *Start from* picker (open state, a filter, collapsible groups remembered in the browser, Enter and Escape, click outside to close) and the four color inputs that switch on and off with the form. Both packages were fetched from npm and their integrity checked against the registry's hash (Preact 10.24.3, MIT; htm 3.1.1, Apache-2.0). Together with the hooks module they are **16 KB** and need no build step. In headless Chrome the pieces behaved as the vanilla ones do: filtering, keyboard, outside click, the remembered groups, and the colors turning on when an accessory or an outfit that uses them is chosen.

What it showed:

* **It did not make the code shorter.** Formatted at 80 columns, the two pieces came to about 110 lines in Preact against about 100 in the existing code (including the picker's markup in `index.html` and its listeners in `initBuild`). Template markup in `htm` takes the same space as the template strings it replaces.
* **It did remove a class of code, not lines.** The sync functions disappear: changing a value re-draws what depends on it, so "enable the colors when an accessory is chosen" is a line in the color component, not a call someone has to remember in `setField`.
* **It has pitfalls of its own.** `<details>` is controlled by the browser as well as the framework: the first version lost the "collapsed" state because the framework and the element disagreed about who owned `open`. It was fixed by making the component the owner, which is the right pattern but is a thing to learn. Escaping becomes automatic, which removes the 106 `esc()` calls and a whole way to make a mistake.
* **A second runtime to carry.** Two more vendored packages with their licences and update notes (htm's Apache-2.0 needs its licence shipped), and a debugging model (components, hooks) that the rest of the code does not use.

## 3. Options

1. **Stay as it is and let `build.js` grow.** No cost now. The next two pages would add roughly 1,500 more lines of the same style and the same synchronisation risk.
2. **Stay vanilla, and tidy it: split large pages into modules, and add a small shared helper layer.** An `html` tagged template that escapes interpolated values by itself (removing the `esc()` calls and making "forgot to escape" impossible), element references returned by the template instead of `$("id")` lookups, and one small store (`get`, `set`, `subscribe`) per page so a page has one `render()` that reads the state. No new dependency. It deals with the measured boilerplate and with most of the synchronisation bugs, at a cost of perhaps 80 lines of shared code.
3. **Preact with htm, vendored, no build step.** Everything in option 2 and more (components, automatic updates), at the cost above. It can be adopted a page at a time, because a Preact component can be mounted into any element.
4. **Lit (web components).** No build step either, a similar size, and good encapsulation, but a different mental model (custom elements and shadow DOM) that fights the app's shared stylesheets and the `rem` sizing rules.
5. **Alpine.js (attributes in the HTML).** Very little code for simple forms, but logic ends up in HTML attributes, which is hard to test and to type-check, and the pure-logic modules (`build-spec.js`) would be bypassed.
6. **A framework with a build step (React, Vue, Svelte, Vite).** A node toolchain, `node_modules` and a bundler in a Python repository, for a loopback tool. It would also change the install, the service and the tests. Not justified by the size of this app.
7. **Server-rendered pages with htmx.** Moves rendering into Python, where the options and rules already live. Attractive for forms, but the viewer, the live preview, the event stream and the clip list are client-heavy, so it would be half the app in one style and half in another.

## 4. Decision

**Do not adopt a framework now. Take option 2: split the pages into modules and add the small shared helper layer, before the Models and Model settings pages are written. Keep option 3 (Preact with htm) as the pre-approved next step, with a stated trigger.**

1. **Split `build.js` into modules by concern** (the form, the start picker, the viewer panel, preview and apply, the loading banner), each exporting one `initX()` as the other panels do. This is the change that most reduces the file size anyone has to hold in their head, and it costs nothing in behaviour.
2. **One `html` helper** that escapes interpolated values by default and has an explicit way to mark a value as trusted markup. New code uses it; existing `esc()` calls are replaced as the modules they are in are touched, not in one sweep.
3. **One small store per writing page** with `get`, `set` and `subscribe`, so a page has one `render()` driven by its state. The existing counters and flags (loading count, viewer job, "settled") become part of that state or stay as they are where they are guarding a race.
4. **`index.html` stops growing with each page.** A page's markup is built by its own module into its tab panel; `index.html` keeps the navigation and the empty panels.
5. **Adopt Preact with htm, vendored, when one of these is true:** a page needs more than a handful of values that depend on one another and the store's `render()` is being worked around (the Model settings role editor is the likely first); a third form-heavy page is being written in the helper style and is repeating the same synchronisation code; or the helper layer starts to grow towards a second framework. The move is page by page, with the pure logic and the CSS untouched. It is a short ADR amendment, not a new design, because this one records the evaluation.
6. **Everything stays testable the way it is today:** pure logic in modules with no DOM, run with node from the test suite; the pages exercised in headless Chrome against a scratch brain, which is what found the real bugs in this work. The new helpers (`html.js`, `store.js`) have node tests of their own.
7. **Types: full JSDoc, checked strictly.** Decided by the user, not left open. Every new or split module starts with `// @ts-check` and describes its data in JSDoc; the shapes the brain sends are in one file (`types.js`); `strict` is on. Checking is gradual by file (`checkJs` is off in `tsconfig.json`, `// @ts-check` opts a file in), so an older module is brought in when it is touched, not in a sweep. `npm run check` (in `status_page/`, `tsc --noEmit`) runs it; a test in the suite runs it when the dev tools are installed. This is **not a build step**: the browser runs the files as written, and the dev-only tools (TypeScript, `@types/three`, Prettier) are in `status_page/package.json`, git-ignored in `node_modules/`. TypeScript source files stay out of scope while the no-build rule stands.
8. **Reduce the code and the room for error, by adding helpers where they earn their place.** A helper is added when it removes repeated code or a class of mistake in code being written, and not before (as `html.js`, `store.js`, `dom.js`, `api.js` and `storage.js` were, from what `build.js` repeated); it is typed, tested and no larger than the code it replaces. The measure is fewer things to keep in step and fewer ways to be wrong, not only fewer lines: types and templates add some lines of their own.

## 5. Consequences

**Good**

* No new runtime, no build step, no change to how the app is installed, served or tested.
* The boilerplate that was measured (escaping, id lookups, hand-written synchronisation) is addressed where it is, for the price of a small shared module.
* The decision is cheap to revisit: the helpers are written to look like what a Preact page would look like (a template, a state, a render), so a later move is mostly mechanical.
* The next two pages start from smaller modules and a safer way to write markup, instead of copying `build.js`.

**Costs and risks**

* The helper layer is our own code to maintain, and it is a risk if it grows. The trigger in decision 5 exists to stop it becoming a framework by accident.
* The code will not get shorter (the proof of concept showed that no approach here does). The gain is fewer things to keep in step and fewer ways to write an unescaped value.
* Automatic updating, which a framework gives for free, is not there: a store with one `render()` redraws more than needed, and has to be careful not to lose focus (render in place, as the form already does).
* The two form-heavy pages are written before the case for a framework is tested on them. That is deliberate: it is better to see whether they need it than to decide now.

## 6. Alternatives considered

The options in section 3 other than option 2 are the alternatives. Two are worth a second sentence. **Doing option 3 now** is defensible and is the main alternative: the proof of concept worked, the runtime is 16 KB, and the sync functions would go. It was not chosen because it did not make the code smaller, it adds two vendored packages and a second way of writing pages to an app that is mostly small read-only dashboards, and the benefit shows only when state gets complicated, which the Models pages may or may not do. **Doing nothing** (option 1) was rejected because the measured boilerplate and the bugs it caused are real and the cheap fix is available before the new pages exist.

## 7. Implementation plan (steps 1 and 2 done for the Build page)

1. **Split `build.js`**, behaviour unchanged, with the existing tests and a headless-Chrome run as the check. Format with Prettier.
2. **Add `html.js` and `store.js`** (pure, with node tests: escaping by default, trusted markup, subscribe and unsubscribe), and move the Build page onto them as it is touched.
3. **Move each page's markup out of `index.html`** into its module (Build first).
4. **Write the Models page on the helpers.** Measure the synchronisation code it needs and note it in this ADR.
5. **Write the Model settings page** and decide on option 3 against the trigger in decision 5.

## 8. Open questions

1. **Should the older read-only modules be converted?** They are small and work; the helpers apply as they are touched. A sweep is not planned.
2. **Types across the brain and the page.** Decision 7 types the page's own code. The JSON the brain sends is described by hand in `types.js`; a schema generated from the brain's pydantic models (`LookSpec` and the options) would keep the two in step and remove a class of bug (a brain older or newer than the page). Not started.
3. **Testing the pages.** The headless-Chrome runs were by hand. Whether a small, optional browser test script belongs in the repository (it would need Chrome and a scratch brain) is open.
4. **Licence files for vendored packages** (Preact MIT, htm Apache-2.0), if option 3 is taken, need to ship next to the code as three.js's does.
