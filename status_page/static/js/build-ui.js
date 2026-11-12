// @ts-check
// The Build page's elements, found once, and the two ways the page shows that it is working.
import { el } from "./dom.js";

export const ui = {
  tab: el("tab-build"),
  loading: el("buildLoading"),
  loadingText: el("buildLoadingText"),
  notice: el("buildNotice"),
  summary: el("buildSummary"),
  start: el("buildStart", HTMLButtonElement),
  startPanel: el("startPanel"),
  startFilter: el("startFilter", HTMLInputElement),
  startGroups: el("startGroups"),
  form: el("buildForm"),
  canvas: el("buildCanvas", HTMLCanvasElement),
  viewerEmpty: el("viewerEmpty"),
  viewerNote: el("viewerNote"),
  clipList: el("clipList"),
  playPause: el("playPause", HTMLButtonElement),
  previewPic: el("previewPic", HTMLImageElement),
  previewHint: el("previewHint"),
  previewCaption: el("previewCaption"),
  previewStale: el("previewStale"),
  previewStatus: el("previewStatus"),
  previewBtn: el("previewBtn", HTMLButtonElement),
  applyBtn: el("applyBtn", HTMLButtonElement),
  applyNote: el("applyNote"),
  applyStatus: el("applyStatus"),
};

let pending = 0;

/**
 * Runs `work` with the page's loading banner up, however many things are loading at once.
 * @template T
 * @param {string} text
 * @param {() => Promise<T>} work
 * @returns {Promise<T>}
 */
export async function busy(text, work) {
  pending += 1;
  ui.loadingText.textContent = text;
  ui.loading.hidden = false;
  ui.tab.setAttribute("aria-busy", "true");
  try {
    return await work();
  } finally {
    pending -= 1;
    if (!pending) {
      ui.loading.hidden = true;
      ui.tab.removeAttribute("aria-busy");
    }
  }
}

/**
 * A line of status under a button: working (a spinner), done, or a problem.
 * @param {HTMLElement} element
 * @param {string} text
 * @param {"" | "busy" | "ok" | "bad"} [kind]
 */
export function setStatus(element, text, kind = "") {
  element.textContent = text;
  element.className = `status${kind ? ` ${kind}` : ""}`;
}

/**
 * A failure is shown on the page, not left in the console.
 * @param {unknown} error
 */
export function report(error) {
  console.error(error);
  ui.notice.hidden = false;
  ui.notice.textContent = `Something went wrong: ${error instanceof Error ? error.message : error}. If the brain was updated, restart it.`;
}
