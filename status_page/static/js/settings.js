// @ts-check
// Model settings page: change how one installed model behaves (docs/ADR_MODEL_SETTINGS_PAGE.md).
// The second page that writes. Everything is edited in a draft (settings-doc.js) and held here until
// Save; the brain validates the whole document and writes it beside the model. A saved change applies
// from the next build of a look on that model, so the page offers to rebuild the current look.
//
// The page is these modules: settings-state, settings-ui, settings-doc (the edits), settings-view
// (the forms) and settings-stage (the viewer). This one starts them and handles what is done to them.
import { ApiError, getJson, write } from "./api.js";
import { all, delegate } from "./dom.js";
import { html, mount } from "./html.js";
import { on } from "./bus.js";
import * as doc from "./settings-doc.js";
import { S, page } from "./settings-state.js";
import {
  drawAccessories,
  initStage,
  offerSwitchOff,
  play,
  playingClip,
  whenPlayed,
  setActive,
  showModel,
  startViewer,
} from "./settings-stage.js";
import { busy, report, setStatus, ui } from "./settings-ui.js";
import * as view from "./settings-view.js";

/**
 * @typedef {import("./settings-state.js").Info} Info
 * @typedef {import("./settings-state.js").ListedModel} ListedModel
 * @typedef {import("./settings-state.js").Checked} Checked
 */

const enc = encodeURIComponent;
const VALIDATE_DELAY_MS = 300;
const SHEET_POLL_MS = 1500;
const SHEET_GIVE_UP_MS = 240_000;
/** @type {ReturnType<typeof setTimeout> | undefined} */
let validateTimer;
let validateSerial = 0;
let sheetSerial = 0;

const dirty = () => !doc.same(S.draft, S.saved);

// --- showing ---------------------------------------------------------------------------------

function renderList() {
  const models = S.models;
  if (!models) {
    mount(
      ui.list,
      html`<div class="empty">${S.unreachable ? "Could not reach the brain." : "Loading…"}</div>`,
    );
    return;
  }
  const held = models.filter((m) => m.held).length;
  const shown = S.heldOnly ? models.filter((m) => m.held) : models;
  ui.summary.textContent = models.length
    ? `${models.length} installed${held ? `, ${held} held` : ""}`
    : "";
  mount(
    ui.list,
    shown.length
      ? html`${shown.map(
          (
            m,
          ) => html`<button type="button" data-model="${m.id}" aria-selected="${m.id === S.id}">
            <span>${m.id}</span>
            ${m.held ? html`<span class="chip">held</span>` : ""}
            ${m.customised ? html`<span class="chip">customised</span>` : ""}
            ${m.credit ? html`<span class="credit">${m.credit}</span>` : ""}
          </button>`,
        )}`
      : models.length
        ? html`<div class="empty">No model is held back.</div>`
        : html`<div class="empty">No model is installed. Install one with <code>python -m brain.appearance.models add-body</code>.</div>`,
  );
}

/** The forms that depend on the draft. Inputs keep their value because only a change redraws them. */
function renderForms() {
  const info = S.info;
  if (!info) return;
  mount(ui.animations, view.animations(info, S.draft, S.showAllClips));
  mount(ui.accessories, view.accessories(info, S.draft, S.focus));
  mount(ui.pose, view.pose(info, S.draft));
  mount(ui.sheetPick, view.sheetPicker(info, S.draft, S.sheetPicks, S.focus));
  mount(ui.avail, view.availability(info));
  renderBar();
  showSheet();
}

function renderBar() {
  const changed = dirty();
  ui.dirty.hidden = !changed;
  ui.revert.disabled = !changed || S.saving;
  const errors = S.checked?.errors ?? [];
  ui.save.disabled = !changed || S.saving || errors.length > 0;
  ui.sheetBtn.disabled = !S.info || errors.length > 0;
  const stale = S.checked?.stale ?? S.info?.stale ?? [];
  if (S.info)
    mount(ui.problems, view.problems(S.info, S.checked, changed ? [] : stale));
}

/** The "doesn't work" button follows the clip on the model and the draft. */
function syncVerdict() {
  const clip = playingClip();
  offerSwitchOff(clip, clip ? doc.clipOn(S.draft, clip) : true);
}

/** A change to the draft: redraw, tell the viewer, and ask the brain what it thinks. */
function changed() {
  renderForms();
  syncVerdict();
  showSheet();
  void drawAccessories(S.draft, S.focus);
  clearTimeout(validateTimer);
  validateTimer = setTimeout(() => void check(), VALIDATE_DELAY_MS);
}

async function check() {
  const info = S.info;
  if (!info) return;
  const mine = ++validateSerial;
  try {
    const verdict = /** @type {Checked} */ (
      await write("POST", "/api/model-settings/validate", {
        body: info.id,
        settings: S.draft,
      })
    );
    if (mine !== validateSerial) return;
    S.checked = verdict;
  } catch (error) {
    if (mine !== validateSerial) return;
    S.checked = {
      ok: false,
      errors: [error instanceof Error ? error.message : String(error)],
      stale: [],
    };
  }
  renderBar();
}

// --- loading ---------------------------------------------------------------------------------

async function loadList() {
  try {
    const data = /** @type {{ models: ListedModel[] }} */ (
      await getJson("/api/model-settings/models")
    );
    page.update((s) => {
      s.models = data.models;
      s.unreachable = false;
    });
  } catch {
    page.update((s) => {
      s.models = null;
      s.unreachable = true;
    });
  }
  renderList();
}

/** @param {string} id */
async function open(id) {
  if (
    id !== S.id &&
    dirty() &&
    !confirm("Discard the changes you have not saved?")
  )
    return;
  await busy("Loading the model…", async () => {
    let info;
    try {
      info = /** @type {Info} */ (
        await getJson(`/api/model-settings/get?body=${enc(id)}`)
      );
    } catch (error) {
      report(error);
      return;
    }
    page.update((s) => {
      s.id = id;
      s.info = info;
      s.saved = doc.clone(info.settings);
      s.draft = doc.clone(info.settings);
      s.checked = null;
      s.focus = null;
      s.sheetPicks = {};
    });
    ui.notice.hidden = true;
    ui.empty.hidden = true;
    ui.editor.hidden = false;
    ui.title.textContent = info.id + (info.held ? " (held)" : "");
    setStatus(ui.status, "");
    ui.rebuild.hidden = true;
    resetSheet();
    renderList();
    renderForms();
    startViewer();
    setActive(true);
    await showModel(info);
    void check();
  });
}

/** After a save: what the brain now has. */
async function reload() {
  const id = S.id;
  if (!id) return;
  const info = /** @type {Info} */ (
    await getJson(`/api/model-settings/get?body=${enc(id)}`)
  );
  page.update((s) => {
    s.info = info;
    s.saved = doc.clone(info.settings);
    s.draft = doc.clone(info.settings);
    s.checked = null;
  });
  renderForms();
  void check();
  await loadList();
}

// --- saving ----------------------------------------------------------------------------------

/** @param {unknown} error */
function say(error) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : String(error);
}

async function save() {
  const info = S.info;
  if (!info || S.saving) return;
  S.saving = true;
  renderBar();
  setStatus(ui.status, "Saving…", "busy");
  try {
    await write("PUT", "/api/model-settings/save", {
      body: info.id,
      settings: S.draft,
    });
    await reload();
    S.saving = false;
    renderBar();
    setStatus(
      ui.status,
      "Saved. It applies from the next build of a look on this model.",
      "ok",
    );
    offerRebuild();
  } catch (error) {
    S.saving = false;
    renderBar();
    setStatus(ui.status, say(error), "bad");
  }
}

function offerRebuild() {
  ui.rebuild.hidden = !S.info?.in_current_look;
  setStatus(ui.rebuildStatus, "");
  ui.rebuildBtn.disabled = false;
}

async function rebuild() {
  const info = S.info;
  if (!info) return;
  ui.rebuildBtn.disabled = true;
  setStatus(ui.rebuildStatus, "Asking the brain to build it again…", "busy");
  try {
    await write("POST", "/api/model-settings/rebuild", { body: info.id });
    setStatus(
      ui.rebuildStatus,
      "Building: the new version appears under Looks when it is done.",
      "ok",
    );
  } catch (error) {
    ui.rebuildBtn.disabled = false;
    setStatus(ui.rebuildStatus, say(error), "bad");
  }
}

/**
 * A record beside the model (hold, credit, landmarks) is saved on its own and takes effect now.
 * @param {string} url
 * @param {Record<string, unknown>} body
 * @param {string} done
 */
async function saveRecord(url, body, done) {
  const info = S.info;
  if (!info) return;
  setStatus(ui.status, "Saving…", "busy");
  try {
    await write("PUT", url, { body: info.id, ...body });
    const keep = doc.clone(S.draft);
    await reload();
    page.update((s) => {
      s.draft = keep; // the settings being edited are not touched by a record
    });
    renderForms();
    void check();
    setStatus(ui.status, done, "ok");
  } catch (error) {
    setStatus(ui.status, say(error), "bad");
  }
}

// --- the alignment sheet ---------------------------------------------------------------------

/**
 * The last sheet built for each model, kept here (the picture itself, so the brain's short list of
 * finished previews cannot take it away) with the settings it was built from.
 * @type {Map<string, { url: string, settings: import("./settings-doc.js").Settings, picks: string, at: Date }>}
 */
const sheets = new Map();
let sheetBuilding = false;
const picksKey = () => JSON.stringify(S.sheetPicks);

/** Shows the kept sheet of the chosen model, and says whether the settings have changed since. */
function showSheet() {
  const kept = S.info ? sheets.get(S.info.id) : undefined;
  if (!kept) {
    ui.sheetPic.hidden = true;
    ui.sheetHint.hidden = false;
    setStatus(ui.sheetStatus, "");
    return;
  }
  if (ui.sheetPic.getAttribute("src") !== kept.url) ui.sheetPic.src = kept.url;
  ui.sheetPic.hidden = false;
  ui.sheetHint.hidden = true;
  if (sheetBuilding) return;
  const time = kept.at.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
  if (doc.same(kept.settings, S.draft) && kept.picks === picksKey())
    setStatus(
      ui.sheetStatus,
      `Built at ${time} from the settings as they are now. Nothing was published.`,
      "ok",
    );
  else
    setStatus(
      ui.sheetStatus,
      `Out of date: built at ${time}, and the settings have changed since. Build it again to check.`,
      "bad",
    );
}

function resetSheet() {
  sheetSerial += 1;
  sheetBuilding = false;
  showSheet();
}

async function buildSheet() {
  const info = S.info;
  if (!info) return;
  const mine = ++sheetSerial;
  ui.sheetBtn.disabled = true;
  sheetBuilding = true;
  setStatus(ui.sheetStatus, "Building the sheet…", "busy");
  try {
    const sent = doc.clone(S.draft);
    const sentPicks = picksKey();
    const started = /** @type {{ id: string }} */ (
      await write("POST", "/api/model-settings/alignment", {
        body: info.id,
        settings: sent,
        ...(Object.keys(S.sheetPicks).length ? { items: S.sheetPicks } : {}),
      })
    );
    const deadline = Date.now() + SHEET_GIVE_UP_MS;
    while (mine === sheetSerial && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, SHEET_POLL_MS));
      if (mine !== sheetSerial) return;
      const state = /** @type {import("./types.js").PreviewInfo} */ (
        await getJson(`/api/build/preview/${started.id}`)
      );
      if (state.state === "failed" || state.state === "cancelled") {
        setStatus(
          ui.sheetStatus,
          state.error ?? "The sheet could not be built.",
          "bad",
        );
        return;
      }
      if (state.state === "ready") {
        const picture = await fetch(`/api/build/preview/${started.id}/picture`);
        if (!picture.ok) throw new Error("The picture could not be read.");
        const url = URL.createObjectURL(await picture.blob());
        const old = sheets.get(info.id);
        if (old) URL.revokeObjectURL(old.url);
        sheets.set(info.id, {
          url,
          settings: doc.clone(sent),
          picks: sentPicks,
          at: new Date(),
        });
        sheetBuilding = false;
        if (S.info?.id === info.id) showSheet();
        return;
      }
    }
    if (mine === sheetSerial)
      setStatus(
        ui.sheetStatus,
        "The sheet is taking too long; try again.",
        "bad",
      );
  } catch (error) {
    if (mine === sheetSerial) setStatus(ui.sheetStatus, say(error), "bad");
  } finally {
    if (mine === sheetSerial) sheetBuilding = false;
    if (mine === sheetSerial)
      ui.sheetBtn.disabled = (S.checked?.errors.length ?? 0) > 0;
  }
}

// --- what is done to the forms ---------------------------------------------------------------

/** @param {HTMLElement} control */
const field = (control) => /** @type {HTMLInputElement} */ (control);

/**
 * Edits the draft for one control that changed.
 * @param {HTMLElement} control
 */
function edit(control) {
  const d = S.draft;
  const info = S.info;
  if (!info) return;
  const { act, name, role, socket, item, key } = control.dataset;
  const input = field(control);
  switch (act) {
    case "library":
      doc.setLibrary(d, String(name), input.checked);
      break;
    case "clip":
      doc.setClip(d, String(name), input.checked);
      break;
    case "role-mode": {
      const mode = /** @type {doc.RoleMode} */ (input.value);
      const seed = view.defaultsFor(info, d, String(role));
      const first = view.usableClips(info, d)[0];
      if (mode === "chosen" && !seed.length && !first) {
        setStatus(ui.status, "There is no animation to choose from.", "bad");
        break;
      }
      doc.setRoleMode(d, String(role), mode, seed.length ? seed : [first]);
      break;
    }
    case "role-add":
    case "clip-role": {
      const target = act === "role-add" ? String(role) : input.value;
      const clip =
        act === "role-add" ? input.value : String(control.dataset.clip);
      if (!target || !clip) return;
      assign(target, clip);
      break;
    }
    case "role-clip":
      doc.setRoleClip(d, String(role), Number(control.dataset.i), input.value);
      break;
    case "sheet-pick": {
      const value = input.value;
      const key = String(socket);
      if (value === "") delete S.sheetPicks[key];
      else S.sheetPicks[key] = value === view.NOTHING ? null : value;
      renderForms();
      return;
    }
    case "socket":
      doc.setSocket(d, String(socket), input.checked);
      break;
    case "item":
      doc.setItem(
        d,
        String(socket),
        String(item),
        input.checked,
        info.choices.items[String(socket)] ?? [],
      );
      break;
    case "place-field":
      editPlacement(control);
      break;
    case "pose":
      editPose(input, String(key));
      break;
    case "relax":
      doc.setPose(d, "relax_arms", input.checked);
      break;
    case "roll": {
      const degrees = doc.parseNumber(input.value);
      if (degrees === null || Number.isNaN(degrees)) input.classList.add("bad");
      else doc.setRoll(d, String(control.dataset.bone), degrees);
      break;
    }
    default:
      return;
  }
  changed();
}

/**
 * Put `clip` into `role`: a role left to the rules starts from what the rules would use, a role that
 * is off starts again from this clip, and a clip already in the role is not added twice.
 * @param {string} role
 * @param {string} clip
 */
function assign(role, clip) {
  const info = S.info;
  if (!info) return;
  const d = S.draft;
  const mode = doc.roleMode(d, role);
  if (mode === "off") doc.setRoleMode(d, role, "chosen", [clip]);
  else if (mode === "auto")
    doc.setRoleMode(d, role, "chosen", view.defaultsFor(info, d, role));
  const list = doc.roleClips(d, role);
  if (!list.includes(clip)) doc.setRoleClip(d, role, list.length, clip);
  setStatus(ui.status, `${clip} added to ${role}.`, "ok");
}

/** @param {HTMLElement} control */
function editPlacement(control) {
  const panel = control.closest(".placement");
  if (!(panel instanceof HTMLElement)) return;
  const { socket, item } = panel.dataset;
  /** @type {Record<string, number>} */
  const v = {};
  let bad = false;
  for (const input of all(panel, "[data-field]")) {
    const n = doc.parseNumber(/** @type {HTMLInputElement} */ (input).value);
    input.classList.toggle("bad", n === null || Number.isNaN(n));
    if (n === null || Number.isNaN(n)) bad = true;
    else v[String(input.dataset.field)] = n;
  }
  if (bad) return;
  doc.setPlacement(S.draft, String(socket), String(item), {
    offset: [v.ox, v.oy, v.oz],
    scale: v.scale,
    rotation: [v.rx, v.ry, v.rz],
  });
}

/**
 * @param {HTMLInputElement} input
 * @param {string} key
 */
function editPose(input, key) {
  const n = doc.parseNumber(input.value);
  input.classList.toggle("bad", n !== null && Number.isNaN(n));
  if (n !== null && Number.isNaN(n)) return;
  doc.setPose(
    S.draft,
    /** @type {"face_yaw" | "emissive_gain" | "arm_spread"} */ (key),
    n,
  );
}

/** @param {HTMLElement} button */
function click(button) {
  const d = S.draft;
  const info = S.info;
  if (!info) return;
  const { act, role, socket, item } = button.dataset;
  switch (act) {
    case "play":
      if (button.dataset.clip) void play(button.dataset.clip);
      return;
    case "sheet-all":
      S.sheetPicks = {};
      renderForms();
      return;
    case "sheet-none":
      S.sheetPicks = Object.fromEntries(
        info.choices.sockets.map((name) => [name, null]),
      );
      renderForms();
      return;
    case "sheet-only": {
      const open = S.focus;
      if (!open) return;
      S.sheetPicks = Object.fromEntries(
        info.choices.sockets.map((name) => [
          name,
          name === open.socket ? open.item : null,
        ]),
      );
      renderForms();
      return;
    }
    case "toggle-all-clips":
      S.showAllClips = !S.showAllClips;
      break;
    case "clip-del": {
      const i = Number(button.dataset.i);
      if (doc.roleClips(d, String(role)).length <= 1)
        doc.setRoleMode(d, String(role), "auto");
      else doc.removeRoleClip(d, String(role), i);
      break;
    }
    case "clip-up":
      doc.moveRoleClipUp(d, String(role), Number(button.dataset.i));
      break;
    case "place":
      S.focus =
        S.focus && S.focus.socket === socket && S.focus.item === item
          ? null
          : { socket: String(socket), item: String(item) };
      break;
    case "place-reset":
      doc.setPlacement(d, String(socket), String(item), doc.IDENTITY);
      break;
    case "roll-add": {
      const bone = /** @type {HTMLInputElement} */ (
        document.getElementById("msRollBone")
      );
      const degrees = doc.parseNumber(
        /** @type {HTMLInputElement} */ (document.getElementById("msRollDeg"))
          .value,
      );
      if (!bone.value.trim() || degrees === null || Number.isNaN(degrees)) {
        setStatus(
          ui.status,
          "Give a bone name and a number of degrees.",
          "bad",
        );
        return;
      }
      doc.setRoll(d, bone.value.trim(), degrees);
      break;
    }
    case "roll-del":
      doc.setRoll(d, String(button.dataset.bone), null);
      break;
    case "remove-stale":
      void removeStale();
      return;
    case "hold": {
      const reason = /** @type {HTMLInputElement} */ (
        document.getElementById("msHoldReason")
      ).value.trim();
      if (!reason) {
        setStatus(ui.status, "Say why it is held back, in a line.", "bad");
        return;
      }
      void saveRecord(
        "/api/model-settings/hold",
        { reason },
        "Held back. It is no longer offered for a look.",
      );
      return;
    }
    case "release":
      void saveRecord("/api/model-settings/hold", {}, "Released.");
      return;
    case "credit-save":
      void saveRecord(
        "/api/model-settings/credits",
        { credits: readCredits() },
        "Credit saved.",
      );
      return;
    case "face-save":
      void saveRecord(
        "/api/model-settings/face",
        { face: readFace() },
        "Landmarks saved. They apply from the next build.",
      );
      return;
    default:
      return;
  }
  changed();
}

function readCredits() {
  /** @type {Record<string, unknown>} */
  const credits = {};
  for (const input of all(ui.avail, "[data-act=credit]"))
    credits[String(input.dataset.key)] = /** @type {HTMLInputElement} */ (
      input
    ).value;
  const notes = /** @type {HTMLTextAreaElement | null} */ (
    ui.avail.querySelector("[data-act=credit-notes]")
  );
  credits.notes = (notes?.value ?? "")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  return credits;
}

function readFace() {
  /** @type {Record<string, Record<string, number>>} */
  const face = {};
  for (const input of all(ui.pose, "[data-act=face]")) {
    const n = doc.parseNumber(/** @type {HTMLInputElement} */ (input).value);
    if (n === null || Number.isNaN(n)) continue;
    (face[String(input.dataset.part)] ??= {})[String(input.dataset.k)] = n;
  }
  return face;
}

async function removeStale() {
  const info = S.info;
  if (!info) return;
  if (dirty()) {
    setStatus(
      ui.status,
      "Save or discard your changes first; removing works on the saved settings.",
      "bad",
    );
    return;
  }
  setStatus(ui.status, "Removing…", "busy");
  try {
    await write("DELETE", `/api/model-settings/stale?body=${enc(info.id)}`);
    await reload();
    setStatus(ui.status, "Removed.", "ok");
  } catch (error) {
    setStatus(ui.status, say(error), "bad");
  }
}

function revert() {
  page.update((s) => {
    s.draft = doc.clone(s.saved);
    s.focus = null;
  });
  setStatus(ui.status, "");
  changed();
}

export function initSettings() {
  initStage();
  ui.heldOnly.addEventListener("change", () => {
    S.heldOnly = ui.heldOnly.checked;
    renderList();
  });
  whenPlayed(syncVerdict);
  ui.verdictBtn.addEventListener("click", () => {
    const clip = playingClip();
    if (!clip) return;
    doc.setClip(S.draft, clip, !doc.clipOn(S.draft, clip));
    changed();
  });
  delegate(ui.list, "click", "[data-model]", (button) => {
    if (button.dataset.model) void open(button.dataset.model);
  });
  delegate(ui.editor, "change", "[data-act]", (control) => {
    if (
      control.dataset.act === "credit" ||
      control.dataset.act === "credit-notes" ||
      control.dataset.act === "face"
    )
      return;
    edit(control);
  });
  delegate(ui.editor, "click", "button[data-act]", click);
  ui.save.addEventListener("click", () => void save());
  ui.revert.addEventListener("click", revert);
  ui.rebuildBtn.addEventListener("click", () => void rebuild());
  ui.sheetBtn.addEventListener("click", () => void buildSheet());
  addEventListener("beforeunload", (event) => {
    if (dirty()) event.preventDefault();
  });
  on("tab", (/** @type {string} */ name) => {
    if (name === "settings") {
      startViewer();
      if (!S.models) void loadList();
    }
    setActive(name === "settings" && !!S.info);
  });
}
