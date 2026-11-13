// @ts-check
// The Model settings page's viewer: the body, a clip played on it, and the accessories that are
// model files placed as the draft says. Same viewer as Build's (viewer.js).
import { getJson } from "./api.js";
import { all, delegate } from "./dom.js";
import { attrs, html, mount } from "./html.js";
import { createViewer } from "./viewer.js";
import { ui } from "./settings-ui.js";
import * as doc from "./settings-doc.js";

/**
 * @typedef {import("./settings-state.js").Info} Info
 * @typedef {import("./settings-doc.js").Settings} Settings
 * @typedef {import("./types.js").Options} Options
 */

/** @type {ReturnType<typeof createViewer> | null} */
let viewer = null;
let job = 0; // a load that finishes after a newer one started is dropped
let drawJob = 0;
/** @type {Options | null} */
let options = null; // each item's own placement, from Build's options
/** @type {string | null} */
let shownBody = null;
/** @type {Info | null} */
let shownInfo = null;
const enc = encodeURIComponent;

/** @param {string} text */
const note = (text) => {
  ui.viewerNote.textContent = text;
};

/** The words over the picture; "" shows the picture. @param {string} text */
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

/** Only draws while the page is on show. @param {boolean} on */
export const setActive = (on) => viewer?.setActive(on);

/**
 * Shows `info`'s body and lists its animations. A held model is not served to the viewer.
 * @param {Info} info
 */
export async function showModel(info) {
  const mine = ++job;
  shownInfo = info;
  shownBody = null;
  setClips(info);
  if (!viewer) return;
  if (info.held) {
    await viewer.setModel(null);
    message(
      "This model is held back, so the viewer does not show it. Release it to see it here.",
    );
    note("");
    return;
  }
  message("Loading the model…");
  const ok = await viewer.setModel(
    `/api/build/model/body?body=${enc(info.id)}`,
  );
  if (mine !== job) return;
  if (!ok) {
    message(
      "This model cannot be shown here (it may be a .blend, which a browser cannot show). Use the alignment sheet.",
    );
    note("");
    return;
  }
  message("");
  shownBody = info.id;
  const first = info.choices.libraries.flatMap((lib) =>
    lib.clips.filter((c) => c.roles.includes("idle")).map((c) => c.name),
  )[0];
  if (first) await play(first);
  else
    note(
      info.choices.has_rig
        ? ""
        : "Shape only: no animation library fits this model's rig.",
    );
}

/** @param {Info} info */
function setClips(info) {
  const rows = info.choices.libraries.flatMap((lib) =>
    lib.clips.map((clip) => ({ lib: lib.name, ...clip })),
  );
  mount(
    ui.clipList,
    rows.length
      ? html`${rows.map((clip) => {
          const tag = attrs({
            type: "button",
            class: "clip",
            role: "option",
            "aria-selected": "false",
            "data-clip": clip.name,
          });
          return html`<button ${tag}><span class="cname">${clip.name}</span
            ><span class="roles">${clip.roles.length ? clip.roles.join(", ") : "no role"}</span></button>`;
        })}`
      : html`<div class="empty-list">no animations</div>`,
  );
  ui.playPause.disabled = !rows.length;
}

/** The clip on the model now, or null. @type {string | null} */
let playing = null;
export const playingClip = () => playing;

/**
 * Offers to switch off the clip being watched: the user has seen it on the model.
 * @param {string | null} clip
 * @param {boolean} isOn
 */
export function offerSwitchOff(clip, isOn) {
  ui.verdict.hidden = !clip;
  if (!clip) return;
  ui.verdictText.textContent = isOn
    ? `Watching ${clip}.`
    : `${clip} is switched off for this model.`;
  ui.verdictBtn.textContent = isOn
    ? "Doesn't work on this model: switch it off"
    : "Switch it back on";
}

/**
 * Plays a clip of the first library that has it.
 * @param {string} clip
 */
export async function play(clip) {
  const info = shownInfo;
  if (!viewer || !info || !shownBody) return;
  const lib = info.choices.libraries.find((l) =>
    l.clips.some((c) => c.name === clip),
  );
  if (!lib) return;
  for (const button of all(ui.clipList, ".clip")) {
    const chosen = button.dataset.clip === clip;
    button.setAttribute("aria-selected", String(chosen));
    if (chosen) button.scrollIntoView({ block: "nearest" });
  }
  const dropped = await viewer.play(
    `/api/build/model/library?body=${enc(info.id)}&library=${enc(lib.name)}`,
    clip,
  );
  ui.playPause.textContent = viewer.isPaused() ? "Play" : "Pause";
  playing = clip;
  onPlayed?.(clip);
  note(
    `${clip}, live.` +
      (dropped
        ? ` ${dropped} of its tracks are for bones this model lacks and are left out, as in the build.`
        : "") +
      " Accessories that are model files follow the placements below.",
  );
}

/**
 * Draws the accessories that are model files, placed as `draft` says: the item whose placement is
 * open, and any other model-file item with a placement. A parametric item exists only in Blender.
 * @param {Settings} draft
 * @param {{ socket: string, item: string } | null} focus
 */
export async function drawAccessories(draft, focus) {
  const info = shownInfo;
  if (!viewer || !info || !shownBody) return;
  const mine = ++drawJob;
  if (!options) {
    try {
      options = await getJson("/api/build/options");
    } catch {
      return;
    }
  }
  const body = options?.bodies.find((b) => b.id === info.id);
  if (!body || mine !== drawJob) return;
  const wanted = [];
  const placed = draft.accessories?.placement ?? {};
  /** @type {Array<[string, string]>} */
  const candidates = [];
  if (focus) candidates.push([focus.socket, focus.item]);
  for (const [socket, items] of Object.entries(placed))
    for (const item of Object.keys(items)) candidates.push([socket, item]);
  const seen = new Set();
  for (const [socket, item] of candidates) {
    if (
      seen.has(socket) ||
      !doc.socketOn(draft, socket) ||
      !doc.itemOn(draft, socket, item)
    )
      continue;
    const base = /** @type {Record<string, any>} */ (body.controls.accessories)[
      socket
    ]?.models?.[item];
    if (!base) continue; // not a model file
    seen.add(socket);
    const p = doc.placementOf(draft, socket, item);
    wanted.push({
      socket,
      url: `/api/build/model/accessory?socket=${enc(socket)}&item=${enc(item)}`,
      offset: base.offset.map(
        (/** @type {number} */ v, /** @type {number} */ i) => v + p.offset[i],
      ),
      scale: base.scale * p.scale,
      rotation: base.rotation.map(
        (/** @type {number} */ v, /** @type {number} */ i) =>
          v + (p.rotation[i] * Math.PI) / 180,
      ),
      skin: base.skin,
    });
  }
  await viewer.setAccessories(wanted);
}

/** @type {((clip: string) => void) | null} */
let onPlayed = null;

/** @param {(clip: string) => void} listener  called when a clip starts playing */
export function whenPlayed(listener) {
  onPlayed = listener;
}

export function initStage() {
  delegate(ui.clipList, "click", "[data-clip]", (button) => {
    if (button.dataset.clip) void play(button.dataset.clip);
  });
  ui.playPause.addEventListener("click", () => {
    if (!viewer) return;
    viewer.setPaused(!viewer.isPaused());
    ui.playPause.textContent = viewer.isPaused() ? "Play" : "Pause";
  });
}
