// @ts-check
// The Model settings page's elements, found once, and its loading banner.
import { el } from "./dom.js";

export const ui = {
  tab: el("tab-settings"),
  loading: el("msLoading"),
  loadingText: el("msLoadingText"),
  notice: el("msNotice"),
  summary: el("msSummary"),
  list: el("msList"),
  heldOnly: el("msHeldOnly", HTMLInputElement),
  empty: el("msEmpty"),
  editor: el("msEditor"),
  title: el("msTitle"),
  dirty: el("msDirty"),
  save: el("msSave", HTMLButtonElement),
  revert: el("msRevert", HTMLButtonElement),
  status: el("msStatus"),
  rebuild: el("msRebuild"),
  rebuildBtn: el("msRebuildBtn", HTMLButtonElement),
  rebuildStatus: el("msRebuildStatus"),
  problems: el("msProblems"),
  animations: el("msAnimations"),
  accessories: el("msAccessories"),
  pose: el("msPose"),
  avail: el("msAvail"),
  canvas: el("msCanvas", HTMLCanvasElement),
  viewerEmpty: el("msViewerEmpty"),
  viewerNote: el("msViewerNote"),
  clipList: el("msClipList"),
  playPause: el("msPlayPause", HTMLButtonElement),
  verdict: el("msVerdict"),
  verdictText: el("msVerdictText"),
  verdictBtn: el("msVerdictBtn", HTMLButtonElement),
  sheetPick: el("msSheetPick"),
  sheetHint: el("msSheetHint"),
  sheetPic: el("msSheetPic", HTMLImageElement),
  sheetStatus: el("msSheetStatus"),
  sheetBtn: el("msSheetBtn", HTMLButtonElement),
};

let pending = 0;

/**
 * Runs `work` with the page's loading banner up.
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
 * A line of status: working, done, or a problem.
 * @param {HTMLElement} element
 * @param {string} text
 * @param {"" | "busy" | "ok" | "bad"} [kind]
 */
export function setStatus(element, text, kind = "") {
  element.textContent = text;
  element.className = `status${kind ? ` ${kind}` : ""}`;
}

/** A failure is shown on the page. @param {unknown} error */
export function report(error) {
  console.error(error);
  ui.notice.hidden = false;
  ui.notice.textContent = `Something went wrong: ${error instanceof Error ? error.message : error}. If the brain was updated, restart it.`;
}
