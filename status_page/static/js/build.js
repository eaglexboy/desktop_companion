// @ts-check
// Build page: make the companion's look by hand (docs/ADR_BUILD_YOUR_LOOK.md). The only page
// that writes: Preview asks the brain for a throwaway build and picture, Apply makes the spec the
// next look (a normal new version). Everything else here only reads.
//
// The page is these modules: build-state (what it knows), build-ui (its elements), build-picker
// (Start from), build-form (the controls), build-stage (the viewer and its animations) and
// build-preview (Preview and Apply). This one starts them and loads what they show.
import { getJson } from "./api.js";
import { on } from "./bus.js";
import { defaultSpec, fromStored } from "./build-spec.js";
import { initForm, renderForm } from "./build-form.js";
import { initPicker } from "./build-picker.js";
import { initPreview, showSummary } from "./build-preview.js";
import { S, bodyById, page } from "./build-state.js";
import {
  initStage,
  setActive,
  startViewer,
  updateViewer,
} from "./build-stage.js";
import { busy, report, ui } from "./build-ui.js";
import { html, mount } from "./html.js";

/**
 * @typedef {import("./types.js").Spec} Spec
 * @typedef {import("./types.js").Options} Options
 * @typedef {import("./types.js").Looks} Looks
 * @typedef {import("./types.js").BrainEvent} BrainEvent
 */

/**
 * Starts the form from a look you have had ("v:<n>") or a new look on a body ("body:<id>").
 * @param {string} value
 */
async function startFrom(value) {
  const options = S.options;
  if (!value || !options) return;
  await busy("Loading the look…", async () => {
    ui.notice.hidden = true;
    /** @type {Spec | null} */
    let spec = null;
    if (value.startsWith("body:")) {
      const body = bodyById(value.slice(5));
      spec = body ? defaultSpec(body) : null;
      page.update((s) => {
        s.base = null; // nothing to compare with: a new look can always be applied
      });
    } else {
      const stored = await getJson(
        `/api/build/spec?version=${value.slice(2)}`,
      ).catch(() => null);
      const wanted = /** @type {{ look_spec?: Partial<Spec> } | null} */ (
        stored
      )?.look_spec;
      const body = wanted?.base_template
        ? bodyById(wanted.base_template)
        : null;
      if (wanted && !body) {
        ui.notice.hidden = false;
        ui.notice.textContent = `The body ${wanted.base_template} is not available now (held or removed), so this starts a new look on another body.`;
      }
      spec =
        body && wanted
          ? fromStored(wanted, body)
          : defaultSpec(options.bodies[0]);
      const loaded = body && wanted ? structuredClone(spec) : null;
      page.update((s) => {
        s.base = loaded;
      });
    }
    page.update((s) => {
      s.spec = spec;
    });
    renderForm();
    await updateViewer();
  });
}

/** What the page offers: what each body can use, and the looks there have been. */
async function load() {
  await busy("Loading what you can build with…", async () => {
    try {
      const [options, looks] = await Promise.all([
        getJson("/api/build/options"),
        getJson("/api/looks"),
      ]);
      page.update((s) => {
        s.options = /** @type {Options} */ (options);
        s.looks = /** @type {Looks} */ (looks);
        s.unreachable = false;
      });
    } catch {
      page.update((s) => {
        s.options = null;
        s.unreachable = true;
      });
      mount(
        ui.form,
        html`<div class="empty">
          Could not reach the brain, so the choices are not known. Try again
          when it is up.
        </div>`,
      );
      return;
    }
    showSummary();
    ui.start.disabled = false;
  });
}

export function initBuild() {
  initForm();
  initStage();
  initPicker((value) => startFrom(value).catch(report));
  initPreview((listener) => on("stream", listener));
  on("tab", (/** @type {string} */ name) => {
    if (name === "build") {
      startViewer();
      if (!S.options) void load();
    }
    setActive(name === "build");
  });
}
