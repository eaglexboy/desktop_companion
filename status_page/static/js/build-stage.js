// @ts-check
// The Build page's stage: the 3D viewer, the list of animations beside it, and the accessories the
// viewer draws. The viewer shows the installed body, its outfit and how it moves; what only a build
// can show (the parametric accessories, colors, proportions) is left to Preview.
import { getJson } from "./api.js";
import { all, delegate } from "./dom.js";
import { attrs, html, mount } from "./html.js";
import { SOCKETS, defaultClip } from "./build-spec.js";
import { S, currentBody, page } from "./build-state.js";
import { busy, ui } from "./build-ui.js";
import { createViewer } from "./viewer.js";

/**
 * @typedef {import("./types.js").Animations} Animations
 * @typedef {import("./types.js").ClipRow} ClipRow
 */

/** @type {ReturnType<typeof createViewer> | null} */
let viewer = null;
let job = 0; // a load that finishes after a newer one started is dropped
const enc = encodeURIComponent;

/** @param {string} text */
const note = (text) => {
  ui.viewerNote.textContent = text;
};

/**
 * The words over the picture while there is nothing to show; "" shows the picture.
 * @param {string} text
 */
const message = (text) => {
  ui.viewerEmpty.textContent = text;
  ui.viewerEmpty.hidden = !text;
};

export function startViewer() {
  if (viewer) return;
  try {
    viewer = createViewer(
      ui.canvas,
      (status) => status.error && message(status.error),
    );
  } catch (error) {
    message(
      `The 3D viewer is not available in this browser (${error instanceof Error ? error.message : error}).`,
    );
  }
}

/** Only draws while the Build page is on show. @param {boolean} on */
export const setActive = (on) => viewer?.setActive(on);

/** Shows the spec's hair colors on the model: its eyebrows and eyelashes, and a hair or beard model. */
export function showHair() {
  if (!S.spec) return;
  viewer?.setHairColor(S.spec.palette.hair);
  viewer?.setHairstyleColor(S.spec.palette.hairstyle);
}

/** Shows the body and outfit of the current spec, and plays its idle. */
export async function updateViewer() {
  const { spec } = S;
  if (!viewer || !spec) return;
  const mine = ++job;
  const body = spec.base_template;
  page.update((s) => {
    s.settled = false;
  });
  await busy("Loading the model…", async () => {
    /** @type {Animations} */
    let info;
    try {
      info = await getJson(`/api/build/animations?body=${enc(body)}`);
    } catch {
      message("Could not ask the brain about this body's animations.");
      return;
    }
    if (mine !== job || !viewer) return;
    if (!info.model) {
      await viewer.setModel(null);
      setClips([]);
      message(
        "This body is built in, so there is no model to show here. Use Preview to see how it looks.",
      );
      note("");
      await syncAccessories();
      return;
    }
    message("Loading the model…");
    showHair();
    const outfit = spec.wardrobe.outfit;
    const shown = await viewer.setModel(
      `/api/build/model/body?body=${enc(body)}`,
      outfit !== "none"
        ? `/api/build/model/outfit?body=${enc(body)}&outfit=${enc(outfit)}`
        : null,
    );
    if (mine !== job) return;
    if (!shown) {
      message("The model could not be shown.");
      return;
    }
    message("");
    await syncAccessories();
    setClips(
      info.libraries.flatMap((library) =>
        library.clips.map((clip) => ({
          library: library.name,
          gaps: library.gaps,
          ...clip,
        })),
      ),
    );
    const first = defaultClip(info.libraries);
    if (!first) {
      note("Shape only: no animation library fits this body's rig.");
      return;
    }
    await selectClip(
      S.clips.findIndex(
        (c) => c.library === first.library && c.name === first.clip,
      ),
    );
  });
}

/**
 * Draws the accessories that are model files (the hair and beard) on the shown body, and notes the
 * rest as Preview only. A parametric accessory exists only inside Blender.
 */
export async function syncAccessories() {
  const { spec } = S;
  const body = currentBody();
  if (!spec || !body) return;
  const wanted = SOCKETS.flatMap((socket) => {
    const item = spec.accessories[socket];
    const placement = item && body.controls.accessories[socket].models[item];
    return placement
      ? [
          {
            socket,
            url: `/api/build/model/accessory?socket=${enc(socket)}&item=${enc(item)}`,
            ...placement,
          },
        ]
      : [];
  });
  const drawn = viewer ? await viewer.setAccessories(wanted) : [];
  if (spec !== S.spec) return; // the form moved on while it loaded
  page.update((s) => {
    s.shown = new Set(drawn);
    s.settled = true;
  });
}

/**
 * What a clip says it is for. An animation no role uses is never played for this model, so it is not
 * offered, unless it is a building block of one that is (the burpee a workout is made of).
 * @param {ClipRow} clip
 */
function clipRole(clip) {
  const parts = clip.part_of ?? [];
  const says = [
    clip.roles.length > 0 &&
      `${clip.roles.length > 1 ? "roles" : "role"}: ${clip.roles.join(", ")}`,
    parts.length > 0 &&
      `part of ${parts.map((p) => `${p.clip} (${p.roles.join(", ")})`).join(", ")}`,
  ].filter(Boolean);
  return says.length ? says.join(" · ") : null;
}

/** @param {ClipRow[]} clips */
function setClips(clips) {
  page.update((s) => {
    s.clips = clips;
  });
  const several = new Set(clips.map((c) => c.library)).size > 1;
  const rows = clips.map((clip, i) => {
    const role = clipRole(clip);
    const heading =
      several &&
      clip.library !== clips[i - 1]?.library &&
      html`<div class="clip-group">${clip.library}</div>`;
    const tag = attrs({
      type: "button",
      class: `clip${role ? "" : " unused"}`,
      role: "option",
      "aria-selected": "false",
      "data-clip": i,
      disabled: !role,
      title: role ? null : "No role uses this animation for this model.",
    });
    return html`${heading}<button ${tag}>
        <span class="cname">${clip.name}</span
        ><span class="roles">${role ?? "not used by any role"}</span>
      </button>`;
  });
  mount(
    ui.clipList,
    clips.length
      ? html`${rows}`
      : html`<div class="empty-list">no animations</div>`,
  );
  ui.playPause.disabled = !clips.length;
}

/** @param {number} index */
async function selectClip(index) {
  const clip = S.clips[index];
  const { spec } = S;
  if (!clip || !viewer || !spec) return;
  for (const button of all(ui.clipList, ".clip")) {
    const chosen = Number(button.dataset.clip) === index;
    button.setAttribute("aria-selected", String(chosen));
    if (chosen) button.scrollIntoView({ block: "nearest" });
  }
  const dropped = await viewer.play(
    `/api/build/model/library?body=${enc(spec.base_template)}&library=${enc(clip.library)}`,
    clip.name,
    clip.speed,
  );
  ui.playPause.textContent = viewer.isPaused() ? "Play" : "Pause";
  note(
    "Body, outfit and animation, live." +
      (dropped
        ? ` ${dropped} of this clip's tracks are for bones this body lacks and are left out, as in the build.`
        : "") +
      " Hair and other accessories that are model files are drawn too; the rest, colors and proportions show in Preview.",
  );
}

export function initStage() {
  delegate(ui.clipList, "click", "[data-clip]", (button) => {
    void selectClip(Number(button.dataset.clip));
  });
  ui.playPause.addEventListener("click", () => {
    if (!viewer) return;
    viewer.setPaused(!viewer.isPaused());
    ui.playPause.textContent = viewer.isPaused() ? "Play" : "Pause";
  });
}
