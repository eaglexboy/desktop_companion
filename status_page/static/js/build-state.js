// @ts-check
// What the Build page knows. Parts of the page that depend on it subscribe to `page`, so a change
// does not have to remember to update every place it shows up.
import { createStore } from "./store.js";

/**
 * @typedef {import("./types.js").Spec} Spec
 * @typedef {import("./types.js").Body} Body
 * @typedef {import("./types.js").Options} Options
 * @typedef {import("./types.js").Looks} Looks
 * @typedef {import("./types.js").ClipRow} ClipRow
 */

/**
 * @typedef {object} PreviewRun
 * @property {string} id
 * @property {Spec} spec  what was built, to tell when the form has moved on since
 * @property {import("./types.js").PreviewState} state
 * @property {string | undefined} previous  the picture this one replaces
 */

/**
 * @typedef {object} BuildState
 * @property {Options | null} options  what each body can use; null until loaded
 * @property {Looks | null} looks
 * @property {string} start  the chosen "Start from" value, "" until one is chosen
 * @property {Spec | null} spec  what the form says
 * @property {Spec | null} base  what was loaded; null for a new look on a body, which can always be applied
 * @property {ClipRow[]} clips  the animations of the shown body
 * @property {PreviewRun | null} preview
 * @property {boolean} applying
 * @property {boolean} unreachable  the brain could not be asked what each body can use
 * @property {Set<string>} shown  the sockets whose accessory the viewer draws
 * @property {boolean} settled  the viewer has said which accessories it draws
 */

/** @type {BuildState} */
const initial = {
  options: null,
  looks: null,
  start: "",
  spec: null,
  base: null,
  clips: [],
  preview: null,
  applying: false,
  unreachable: false,
  shown: new Set(),
  settled: false,
};

export const page = createStore(initial);
export const S = page.state;

/**
 * The options for the body the form is on, or null before there is a form.
 * @returns {Body | null}
 */
export function currentBody() {
  const { options, spec } = S;
  return options?.bodies.find((b) => b.id === spec?.base_template) ?? null;
}

/** The part of the options a body is looked up by. @param {string} id */
export function bodyById(id) {
  return S.options?.bodies.find((b) => b.id === id) ?? null;
}
