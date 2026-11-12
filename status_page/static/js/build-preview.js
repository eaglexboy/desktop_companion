// @ts-check
// Preview and Apply: the only two things the Build page writes. Preview builds a throwaway picture
// of the whole look; Apply makes the form the companion's next look (a normal new version).
import { ApiError, getJson, write } from "./api.js";
import { isDirty, sameSpec, summary, toRequest } from "./build-spec.js";
import { S, page } from "./build-state.js";
import { setStatus, ui } from "./build-ui.js";

/**
 * @typedef {import("./types.js").PreviewInfo} PreviewInfo
 * @typedef {import("./types.js").BrainEvent} BrainEvent
 * @typedef {import("./types.js").Looks} Looks
 */

const POLL_MS = 1_500;
/** @type {Record<string, string>} */
const WORKING = {
  queued: "Waiting for another build to finish…",
  building: "Building your look (about 20 seconds)…",
  rendering: "Drawing the picture…",
};
let timer = 0;

/** What the page says under Apply, which depends on what is loaded and what is changed. */
function applyNote() {
  const { spec, looks } = S;
  if (!spec) return "";
  if (!isDirty(spec, S.base))
    return "Change something to apply it: this is the look you started from.";
  const current = looks?.current;
  const imported = spec.base_template.startsWith("custom:");
  return [
    `Apply builds this as version ${current ? current + 1 : 1} (about 20 seconds). To go back, tell the companion “go back to version ${current ?? 1}”.`,
    looks?.locked &&
      !imported &&
      "The companion is keeping an imported model now; this look ends that, so it may redesign itself again later.",
    imported &&
      "This is an installed model, so the companion keeps it until you tell it that it may change its look.",
  ]
    .filter(Boolean)
    .join(" ");
}

/** The buttons and notes follow the state: what is loaded, what changed, what is running. */
function syncActions() {
  const { spec, preview } = S;
  const working =
    preview && ["queued", "building", "rendering"].includes(preview.state);
  ui.previewBtn.disabled = !spec || !!working;
  ui.applyBtn.disabled = S.applying || !isDirty(spec, S.base);
  ui.previewStale.hidden = !(
    spec &&
    preview?.state === "ready" &&
    !sameSpec(spec, preview.spec)
  );
  ui.applyNote.textContent = applyNote();
}

async function startPreview() {
  const { spec } = S;
  if (!spec) return;
  const copy = structuredClone(spec);
  const previous = S.preview?.id;
  setStatus(ui.previewStatus, "Starting the preview…", "busy");
  try {
    const started = /** @type {PreviewInfo} */ (
      await write("POST", "/api/build/preview", toRequest(copy))
    );
    page.update((s) => {
      s.preview = {
        id: started.id,
        spec: copy,
        state: started.state,
        previous,
      };
    });
  } catch (error) {
    setStatus(
      ui.previewStatus,
      error instanceof ApiError && error.status === 409
        ? "A preview is already being made."
        : `Could not start the preview: ${error instanceof Error ? error.message : error}`,
      "bad",
    );
    return;
  }
  void poll();
}

async function poll() {
  clearTimeout(timer);
  const mine = S.preview;
  if (!mine) return;
  /** @type {PreviewInfo} */
  let info;
  try {
    info = await getJson(`/api/build/preview/${mine.id}`);
  } catch {
    setStatus(
      ui.previewStatus,
      "Lost touch with the brain; trying again…",
      "bad",
    );
    timer = window.setTimeout(poll, POLL_MS);
    return;
  }
  if (S.preview !== mine) return; // another preview started meanwhile
  page.update(() => {
    mine.state = info.state;
  });
  if (info.state === "ready") {
    ui.previewPic.src = `/api/build/preview/${mine.id}/picture`;
    ui.previewPic.hidden = false;
    ui.previewHint.hidden = true;
    ui.previewCaption.textContent = summary(mine.spec);
    setStatus(ui.previewStatus, "Preview ready.", "ok");
    if (mine.previous)
      write("DELETE", `/api/build/preview/${mine.previous}`).catch(() => {});
  } else if (info.state === "failed" || info.state === "cancelled") {
    setStatus(
      ui.previewStatus,
      `The preview failed: ${info.error || info.state}`,
      "bad",
    );
  } else {
    setStatus(ui.previewStatus, WORKING[info.state] ?? info.state, "busy");
    timer = window.setTimeout(poll, POLL_MS);
  }
}

async function apply() {
  const { spec } = S;
  if (!spec) return;
  page.update((s) => {
    s.applying = true;
  });
  setStatus(ui.applyStatus, "Starting the build…", "busy");
  try {
    await write("POST", "/api/build/apply", toRequest(spec));
    setStatus(ui.applyStatus, "Building your look (about 20 seconds)…", "busy");
  } catch (error) {
    page.update((s) => {
      s.applying = false;
    });
    setStatus(
      ui.applyStatus,
      `Could not apply: ${error instanceof Error ? error.message : error}`,
      "bad",
    );
  }
}

/** The looks as the brain has them now, and what the picker and the summary say about them. */
export async function refreshLooks() {
  try {
    const looks = await getJson("/api/looks");
    page.update((s) => {
      s.looks = /** @type {Looks} */ (looks);
      s.start = s.looks.current ? `v:${s.looks.current}` : "";
    });
  } catch {
    return;
  }
  showSummary();
}

export function showSummary() {
  ui.summary.textContent = S.looks?.current
    ? `now version ${S.looks.current}`
    : "";
}

/**
 * How the build ended, from the brain's progress events.
 * @param {BrainEvent} event
 */
function onStream(event) {
  if (!S.applying) return;
  /** @type {Record<string, string>} */
  const says = {
    "appearance.done": `Done: this is now version ${event.data?.version}.`,
    "appearance.failed": `The build failed${event.data?.error ? `: ${event.data.error}` : "."}`,
    "appearance.unchanged": "The companion already looks like that.",
    "appearance.staged": "Built; it will be revealed when you are back.",
  };
  const text = says[event.type];
  if (!text) return;
  const failed = event.type === "appearance.failed";
  page.update((s) => {
    s.applying = false;
    if (!failed) s.base = structuredClone(s.spec); // it is the current look now
  });
  setStatus(ui.applyStatus, text, failed ? "bad" : "ok");
  void refreshLooks();
}

/** @param {(listener: (event: BrainEvent) => void) => void} onEvent  subscribes to the brain's events */
export function initPreview(onEvent) {
  ui.previewBtn.addEventListener("click", () => void startPreview());
  ui.applyBtn.addEventListener("click", () => void apply());
  page.subscribe(syncActions);
  onEvent(onStream);
}
