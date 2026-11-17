// @ts-check
// The Language settings page: the character's language fields and the detection levels, saved by
// the brain to the deploy directory (never config.toml) and applied live. Same shape as the Model
// settings page: edit, see "unsaved changes", Save or Discard.
import { ApiError, getJson, write } from "./api.js";
import { on } from "./bus.js";
import { el } from "./dom.js";
import { esc } from "./util.js";

/**
 * @typedef {object} Profile
 * @property {string} home
 * @property {number} fluency
 * @property {Record<string, number>} fluency_by_language
 * @property {number | null} code_switching
 * @property {string[]} voice_languages
 */

/**
 * @typedef {object} Doc
 * @property {string} default
 * @property {string[]} enabled
 * @property {number} window
 * @property {number} margin
 * @property {number} native_threshold
 * @property {number} wording_floor
 * @property {number} code_switching
 * @property {Profile} profile
 */

/**
 * @typedef {object} View
 * @property {Doc} settings
 * @property {Doc} config_toml
 * @property {string} source  "page" when saved from this page, else "config.toml"
 * @property {string | null} pin
 * @property {string[]} speakable_languages
 */

/**
 * One row of the per-language fluency table.
 * @typedef {object} FluencyRow
 * @property {string} language
 * @property {number} level
 */

const ui = {
  summary: el("lgSummary"),
  notice: el("lgNotice"),
  pin: el("lgPin", HTMLSelectElement),
  pinHint: el("lgPinHint"),
  dirty: el("lgDirty"),
  reset: el("lgReset", HTMLButtonElement),
  revert: el("lgRevert", HTMLButtonElement),
  save: el("lgSave", HTMLButtonElement),
  problems: el("lgProblems"),
  home: el("lgHome", HTMLInputElement),
  fluency: el("lgFluency", HTMLInputElement),
  fluencyOut: el("lgFluencyOut"),
  voiceLanguages: el("lgVoiceLanguages", HTMLInputElement),
  byLanguage: el("lgByLanguage"),
  add: el("lgAddLanguage", HTMLButtonElement),
  useGlobalCs: el("lgUseGlobalCs", HTMLInputElement),
  cs: el("lgCs", HTMLInputElement),
  csOut: el("lgCsOut"),
  effect: el("lgEffect"),
  default: el("lgDefault", HTMLInputElement),
  enabled: el("lgEnabled", HTMLInputElement),
  window: el("lgWindow", HTMLInputElement),
  margin: el("lgMargin", HTMLInputElement),
  native: el("lgNative", HTMLInputElement),
  floor: el("lgFloor", HTMLInputElement),
  tab: el("tab-language"),
};

/** @type {View | null} */
let view = null;
/**
 * The per-language fluency rows being edited, in order.
 * @type {FluencyRow[]}
 */
let rows = [];
let loaded = false;

/** @param {string} text @returns {string[]} */
const list = (text) =>
  text
    .split(/[,\s]+/)
    .map((t) => t.trim())
    .filter(Boolean);

/** A language tag as the brain stores it: es_MX, es-MX and es-mx are one tag. @param {string} text */
const tag = (text) => text.trim().toLowerCase().replace(/_/g, "-");

/** @param {unknown} value @param {number} fallback */
const num = (value, fallback) => {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
};

/** The document the form currently describes. @returns {Doc} */
function readForm() {
  /** @type {Record<string, number>} */
  const byLanguage = {};
  for (const r of rows)
    if (tag(r.language)) byLanguage[tag(r.language)] = r.level;
  return {
    default: tag(ui.default.value) || "en",
    enabled: list(ui.enabled.value).map(tag),
    window: num(ui.window.value, 5),
    margin: num(ui.margin.value, 0.25),
    native_threshold: num(ui.native.value, 80),
    wording_floor: num(ui.floor.value, 20),
    code_switching: ui.useGlobalCs.checked
      ? num(ui.cs.value, 0)
      : (view?.settings.code_switching ?? 0),
    profile: {
      home: tag(ui.home.value),
      fluency: num(ui.fluency.value, 100),
      fluency_by_language: byLanguage,
      code_switching: ui.useGlobalCs.checked ? null : num(ui.cs.value, 0),
      voice_languages: list(ui.voiceLanguages.value).map(tag),
    },
  };
}

/** @param {Doc} doc */
function writeForm(doc) {
  ui.default.value = doc.default;
  ui.enabled.value = doc.enabled.join(", ");
  ui.window.value = String(doc.window);
  ui.margin.value = String(doc.margin);
  ui.native.value = String(doc.native_threshold);
  ui.floor.value = String(doc.wording_floor);
  ui.home.value = doc.profile.home;
  ui.fluency.value = String(doc.profile.fluency);
  ui.voiceLanguages.value = doc.profile.voice_languages.join(", ");
  rows = Object.entries(doc.profile.fluency_by_language).map(
    ([language, level]) => ({ language, level }),
  );
  const own = doc.profile.code_switching;
  ui.useGlobalCs.checked = own === null;
  ui.cs.value = String(own ?? doc.code_switching);
  renderRows();
  refresh();
}

function renderRows() {
  ui.byLanguage.innerHTML = rows.length
    ? rows
        .map(
          (r, i) => `<div class="bylang-row" data-i="${i}">
      <input type="text" value="${esc(r.language)}" aria-label="language" data-f="language" placeholder="es-mx">
      <input type="range" min="0" max="100" step="1" value="${r.level}" aria-label="fluency" data-f="level">
      <span class="out">${r.level}</span>
      <button type="button" data-remove="${i}" aria-label="Remove">Remove</button></div>`,
        )
        .join("")
    : '<div class="muted">none: every other language uses the general level</div>';
}

/** What the current values mean, per language in use. */
function renderEffect() {
  const doc = readForm();
  const home = doc.profile.home || doc.default;
  const languages = [
    ...new Set([
      ...doc.enabled,
      ...Object.keys(doc.profile.fluency_by_language),
    ]),
  ];
  /** @param {string} tag */
  const level = (tag) => {
    const primary = tag.split("-")[0];
    if (primary === home.split("-")[0]) return 100;
    return (
      doc.profile.fluency_by_language[tag] ??
      doc.profile.fluency_by_language[primary] ??
      doc.profile.fluency
    );
  };
  ui.effect.innerHTML = languages
    .map((tag) => {
      const l = level(tag);
      const how =
        l >= doc.native_threshold
          ? "spoken natively"
          : l < doc.wording_floor
            ? `answers mostly in ${esc(home)}, with the odd word`
            : `spoken with the ${esc(home)} accent, still learning`;
      return `<div><b>${esc(tag)}</b> — fluency ${l}: ${how}</div>`;
    })
    .join("");
  ui.fluencyOut.textContent = String(doc.profile.fluency);
  ui.csOut.textContent = String(num(ui.cs.value, 0));
}

/** Key order does not matter. @param {unknown} value @returns {string} */
const canon = (value) =>
  JSON.stringify(value, (_key, v) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? Object.fromEntries(
          Object.entries(v).sort(([a], [b]) => a.localeCompare(b)),
        )
      : v,
  );

/** @param {Doc} a @param {Doc} b */
const same = (a, b) => canon(a) === canon(b);

function refresh() {
  renderEffect();
  const changed = !!view && !same(readForm(), view.settings);
  ui.dirty.hidden = !changed;
  ui.save.disabled = !changed;
  ui.revert.disabled = !changed;
  ui.reset.disabled = !view || view.source !== "page";
}

/** @param {string} text @param {boolean} [bad] */
function say(text, bad = false) {
  ui.notice.textContent = text;
  ui.notice.style.color = bad ? "var(--bad)" : "";
}

/** @param {View} next */
function show(next) {
  view = next;
  ui.summary.textContent =
    next.source === "page"
      ? "saved from this page"
      : "using config.toml (nothing saved here)";
  const options = [
    "auto",
    ...new Set([
      ...next.speakable_languages,
      ...next.settings.enabled,
      ...(next.pin ? [next.pin] : []),
    ]),
  ];
  ui.pin.innerHTML = options
    .map(
      (o) =>
        `<option value="${esc(o)}">${o === "auto" ? "Follow my language" : esc(o)}</option>`,
    )
    .join("");
  ui.pin.value = next.pin ?? "auto";
  ui.pinHint.textContent = next.pin
    ? `pinned to ${next.pin}`
    : "detecting from what you write or say";
  writeForm(next.settings);
}

async function load() {
  try {
    show(await getJson("/api/language/settings"));
    loaded = true;
    say("");
  } catch (e) {
    say(
      `Could not load the language settings (${e instanceof Error ? e.message : e}).`,
      true,
    );
  }
}

/** @param {unknown} e */
function problem(e) {
  const reason =
    e instanceof ApiError || e instanceof Error ? e.message : String(e);
  ui.problems.hidden = false;
  ui.problems.textContent = reason;
  say("Not saved.", true);
}

async function save() {
  ui.problems.hidden = true;
  ui.save.disabled = true;
  say("Saving…");
  try {
    show(
      /** @type {View} */ (
        await write("PUT", "/api/language/settings", readForm())
      ),
    );
    say("Saved and applied. No restart needed.");
  } catch (e) {
    problem(e);
    refresh();
  }
}

async function reset() {
  if (
    !confirm(
      "Forget the settings saved on this page and use config.toml again?",
    )
  )
    return;
  ui.problems.hidden = true;
  try {
    show(/** @type {View} */ (await write("DELETE", "/api/language/settings")));
    say("Back to config.toml.");
  } catch (e) {
    problem(e);
  }
}

/** @param {string} code */
async function pin(code) {
  try {
    show(
      /** @type {View} */ (await write("PUT", "/api/language/pin", { code })),
    );
    say(
      code === "auto" ? "Following your language." : `Now talking in ${code}.`,
    );
  } catch (e) {
    problem(e);
    if (view) ui.pin.value = view.pin ?? "auto";
  }
}

export function initLanguage() {
  on("tab", (/** @type {string} */ name) => {
    if (name === "language" && (!loaded || !isDirty())) void load();
  });
  ui.save.onclick = () => void save();
  ui.reset.onclick = () => void reset();
  ui.revert.onclick = () => view && writeForm(view.settings);
  ui.pin.onchange = () => void pin(ui.pin.value);
  ui.add.onclick = () => {
    rows.push({ language: "", level: num(ui.fluency.value, 100) });
    renderRows();
    refresh();
  };
  for (const input of [
    ui.home,
    ui.fluency,
    ui.voiceLanguages,
    ui.useGlobalCs,
    ui.cs,
    ui.default,
    ui.enabled,
    ui.window,
    ui.margin,
    ui.native,
    ui.floor,
  ])
    input.addEventListener("input", refresh);
  ui.byLanguage.addEventListener("input", (event) => {
    const target = event.target;
    const row =
      target instanceof Element ? target.closest(".bylang-row") : null;
    if (!(target instanceof HTMLInputElement) || !(row instanceof HTMLElement))
      return;
    const entry = rows[Number(row.dataset.i)];
    if (!entry) return;
    if (target.dataset.f === "level") {
      entry.level = num(target.value, entry.level);
      const out = row.querySelector(".out");
      if (out) out.textContent = String(entry.level);
    } else entry.language = target.value;
    refresh();
  });
  ui.byLanguage.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement) || target.dataset.remove === undefined)
      return;
    rows.splice(Number(target.dataset.remove), 1);
    renderRows();
    refresh();
  });
}

function isDirty() {
  return !!view && !same(readForm(), view.settings);
}
