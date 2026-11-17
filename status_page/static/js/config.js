// @ts-check
// The Settings page: config.toml as a form. The Admin app's own process reads and writes the file
// (brain/config_editor.py), so this works with the brain down. Edit, "unsaved changes", Save.
import { ApiError, getJson, write } from "./api.js";
import { on } from "./bus.js";
import { el } from "./dom.js";
import { esc } from "./util.js";

/**
 * One setting, as the server describes it.
 * @typedef {object} Field
 * @property {string[]} path
 * @property {string} key
 * @property {string} kind
 * @property {boolean} set
 * @property {unknown} default
 * @property {string[] | null} choices
 * @property {boolean} nullable
 * @property {string} help
 * @property {string} apply  live, brain (read at brain startup) or status (read at Admin app startup)
 * @property {boolean} secret
 * @property {boolean} locked
 * @property {string} locked_reason
 * @property {unknown} value
 * @property {boolean} [secret_set]
 * @property {number} [min]
 * @property {number} [max]
 * @property {number} [min_exclusive]
 * @property {number} [max_exclusive]
 */

/**
 * @typedef {object} Group
 * @property {string[]} path
 * @property {string} title
 * @property {boolean} locked
 * @property {string} locked_reason
 * @property {Field[]} fields
 */

/**
 * @typedef {object} View
 * @property {string} path
 * @property {string} hash
 * @property {boolean} backup
 * @property {Group[]} groups
 */

/**
 * @typedef {object} Applied
 * @property {string[]} path
 * @property {string} apply
 * @property {boolean} unset
 */

/**
 * An edit not yet saved.
 * @typedef {object} Edit
 * @property {unknown} [value]
 * @property {boolean} [unset]
 * @property {string} [invalid]  why the typed value is not usable
 */

/**
 * What the server is asked to change in one setting.
 * @typedef {object} Change
 * @property {string[]} path
 * @property {unknown} [value]
 * @property {boolean} [unset]
 */

/**
 * @typedef {object} SaveResult
 * @property {View} describe
 * @property {Applied[]} applied
 */

/**
 * @typedef {object} ParseOk
 * @property {unknown} value
 */

/**
 * @typedef {object} ParseError
 * @property {string} error
 */

/**
 * One Settings page: the part of config.toml for its sections, with its own bar and result area.
 * Every page shares one set of edits, so a change on one page is saved with the others.
 * @typedef {object} Page
 * @property {string[]} sections
 * @property {HTMLElement} root
 * @property {Element | null} path
 * @property {Record<string, HTMLElement>} r
 */

/** @type {Page[]} */
const pages = [];

const SKELETON = `
  <div class="status" data-r="notice" role="status" aria-live="polite"></div>
  <div class="cfg-bar">
    <span class="chip stale" data-r="dirty" hidden></span>
    <input type="search" data-r="filter" placeholder="Filter settings…" aria-label="Filter settings">
    <span class="sp"></span>
    <button type="button" data-r="restore" hidden title="Put back the file as it was before the last save from these pages">Restore previous version</button>
    <button type="button" data-r="revert" disabled title="Discard the unsaved changes on every Settings page">Discard changes</button>
    <button type="button" data-r="save" class="primary" disabled title="Saves the changes on every Settings page">Save</button>
  </div>
  <div class="reasoning-note problems warn" data-r="problems" hidden></div>
  <div class="reasoning-note" data-r="result" hidden>
    <div data-r="resultText"></div>
    <button type="button" data-r="restart" hidden>Restart the brain</button>
  </div>
  <div data-r="body"><div class="empty">Loading…</div></div>`;

/** @type {View | null} */
let view = null;
/** Edits by dotted path; absent = unchanged. @type {Map<string, Edit>} */
let edits = new Map();
let loaded = false;

/** @param {Field} f */
const id = (f) => f.path.join(".");

/** Key order does not matter. @param {unknown} value @returns {string} */
const canon = (value) =>
  JSON.stringify(value, (_key, v) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? Object.fromEntries(
          Object.entries(v).sort(([a], [b]) => a.localeCompare(b)),
        )
      : v,
  );

/** @param {Field} f the value as an input shows it */
function shown(f) {
  if (f.kind === "list")
    return /** @type {unknown[]} */ (f.value ?? []).join(", ");
  if (f.kind === "json") return JSON.stringify(f.value ?? {});
  return f.value === null || f.value === undefined ? "" : String(f.value);
}

/**
 * The value an input's text means, or an error.
 * @param {Field} f
 * @param {string} text
 * @returns {ParseOk | ParseError}
 */
function parse(f, text) {
  if (f.kind === "int" || f.kind === "float") {
    if (text.trim() === "")
      return f.nullable ? { value: null } : { error: "a number is needed" };
    const n = Number(text);
    if (!Number.isFinite(n)) return { error: "not a number" };
    if (f.kind === "int" && !Number.isInteger(n))
      return { error: "a whole number is needed" };
    if (f.min !== undefined && n < f.min) return { error: `at least ${f.min}` };
    if (f.max !== undefined && n > f.max) return { error: `at most ${f.max}` };
    if (f.min_exclusive !== undefined && n <= f.min_exclusive)
      return { error: `more than ${f.min_exclusive}` };
    if (f.max_exclusive !== undefined && n >= f.max_exclusive)
      return { error: `less than ${f.max_exclusive}` };
    return { value: n };
  }
  if (f.kind === "list") {
    const items = text
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    const numeric =
      Array.isArray(f.value) &&
      f.value.length > 0 &&
      f.value.every((v) => typeof v === "number");
    if (!numeric) return { value: items };
    const nums = items.map(Number);
    return nums.every(Number.isFinite)
      ? { value: nums }
      : { error: "numbers separated by commas" };
  }
  if (f.kind === "json") {
    try {
      const v = JSON.parse(text || "{}");
      return v && typeof v === "object" && !Array.isArray(v)
        ? { value: v }
        : { error: "a JSON object is needed" };
    } catch {
      return { error: "not valid JSON" };
    }
  }
  if (f.nullable && text === "") return { value: null };
  return { value: text };
}

/** @param {Field} f @param {string} text */
function onInput(f, text) {
  const result = parse(f, text);
  if ("error" in result) edits.set(id(f), { invalid: result.error });
  else if (canon(result.value) === canon(f.value)) edits.delete(id(f));
  else edits.set(id(f), { value: result.value });
}

/** @param {Field} f */
function fieldHtml(f) {
  const name = id(f);
  const chips = [
    `<span class="chip" title="${f.apply === "live" ? "applies at once" : f.apply === "status" ? "read when the Admin app starts" : "read when the brain starts"}">${f.apply === "live" ? "live" : "restart"}</span>`,
    f.set
      ? ""
      : '<span class="chip" title="not in config.toml: the default applies">default</span>',
  ].join("");
  const label = `<span class="name"><b>${esc(f.key)}</b>${chips}</span>`;
  const hint = f.help ? `<span class="hint">${esc(f.help)}</span>` : "";
  if (f.locked) {
    const text = f.secret
      ? f.secret_set
        ? f.value
          ? String(f.value)
          : "set (hidden)"
        : "not set"
      : shown(f);
    return `<div class="cfg-field locked" data-id="${esc(name)}" title="${esc(f.locked_reason)}">${label}
      <input type="text" value="${esc(text)}" disabled aria-label="${esc(f.key)}">
      <span class="hint">${esc(f.locked_reason)}</span></div>`;
  }
  let control;
  if (f.kind === "bool") {
    control = `<label><input type="checkbox" data-f="${esc(name)}" ${f.value ? "checked" : ""}> ${f.value ? "on" : "off"}</label>`;
  } else if (f.kind === "choice") {
    control = `<select data-f="${esc(name)}">${(f.choices ?? [])
      .map(
        (c) =>
          `<option${c === String(f.value) ? " selected" : ""}>${esc(c)}</option>`,
      )
      .join("")}</select>`;
  } else if (f.kind === "json") {
    control = `<textarea data-f="${esc(name)}" spellcheck="false">${esc(shown(f))}</textarea>`;
  } else {
    const num = f.kind === "int" || f.kind === "float";
    control = `<input data-f="${esc(name)}" type="${num ? "number" : "text"}" ${
      num ? `step="${f.kind === "int" ? 1 : "any"}"` : ""
    } ${f.min !== undefined ? `min="${f.min}"` : ""} ${f.max !== undefined ? `max="${f.max}"` : ""} value="${esc(shown(f))}" autocomplete="off">`;
  }
  const reset =
    f.set && f.default !== null
      ? `<button type="button" class="link" data-reset="${esc(name)}">use default (${esc(JSON.stringify(f.default))})</button>`
      : "";
  return `<div class="cfg-field${f.kind === "bool" ? " check" : ""}" data-id="${esc(name)}">${label}${control}${hint}
    <span class="bad-text" data-err="${esc(name)}"></span>${reset}</div>`;
}

/** @param {Page} page @param {Group} g */
const inPage = (page, g) => page.sections.includes(g.path[0] ?? "");

function render() {
  if (!view) return;
  for (const page of pages) {
    if (page.path) page.path.textContent = view.path;
    page.r.restore.hidden = !view.backup;
    const groups = view.groups.filter((g) => inPage(page, g));
    const several = page.sections.length > 1;
    page.r.body.innerHTML = groups.length
      ? groups
          .map((g) => {
            const title =
              several || g.path.length > 1 ? `<h4>${esc(g.title)}</h4>` : "";
            const locked = g.locked
              ? `<p class="hint">🔒 ${esc(g.locked_reason)}</p>`
              : "";
            return `<div class="cfg-group">${title}${locked}<div class="cfg-fields">${g.fields.map(fieldHtml).join("")}</div></div>`;
          })
          .join("")
      : '<div class="empty">Nothing to set here.</div>';
    applyFilter(page);
  }
  refresh();
}

/**
 * The fields a change was made to, as the server wants them.
 * @returns {Change[]}
 */
function changes() {
  /** @type {Change[]} */
  const out = [];
  for (const g of view?.groups ?? [])
    for (const f of g.fields) {
      const e = edits.get(id(f));
      if (!e || e.invalid) continue;
      out.push(
        e.unset
          ? { path: f.path, unset: true }
          : { path: f.path, value: e.value },
      );
    }
  return out;
}

/** How many unsaved edits belong to this page's sections. @param {Page} page */
function editsHere(page) {
  let n = 0;
  for (const key of edits.keys())
    if (page.sections.includes(key.split(".")[0] ?? "")) n += 1;
  return n;
}

function refresh() {
  const total = edits.size;
  const invalid = [...edits.values()].some((e) => e.invalid);
  for (const page of pages) {
    const here = editsHere(page);
    const elsewhere = total - here;
    page.r.dirty.hidden = total === 0;
    page.r.dirty.textContent =
      `${here} unsaved change${here === 1 ? "" : "s"}` +
      (elsewhere ? ` here, ${elsewhere} on other Settings pages` : "");
    /** @type {HTMLButtonElement} */ (page.r.save).disabled =
      total === 0 || invalid;
    /** @type {HTMLButtonElement} */ (page.r.revert).disabled = total === 0;
    for (const row of page.r.body.querySelectorAll(".cfg-field[data-id]")) {
      const key = /** @type {HTMLElement} */ (row).dataset.id ?? "";
      const e = edits.get(key);
      row.classList.toggle("changed", !!e);
      const err = row.querySelector("[data-err]");
      if (err)
        err.textContent =
          e?.invalid ?? (e?.unset ? "will use the default" : "");
      for (const input of row.querySelectorAll(
        "input[data-f], textarea[data-f]",
      ))
        input.classList.toggle("bad", !!e?.invalid);
    }
  }
}

/** @param {Page} page */
function applyFilter(page) {
  const q = /** @type {HTMLInputElement} */ (page.r.filter).value
    .trim()
    .toLowerCase();
  for (const group of page.r.body.querySelectorAll(".cfg-group")) {
    let any = false;
    for (const row of group.querySelectorAll(".cfg-field")) {
      const text =
        `${row.getAttribute("data-id")} ${row.textContent}`.toLowerCase();
      const hit = !q || text.includes(q);
      /** @type {HTMLElement} */ (row).hidden = !hit;
      any = any || hit;
    }
    /** @type {HTMLElement} */ (group).hidden = !any;
  }
}

/** @param {string} text @param {boolean} [bad] */
function say(text, bad = false) {
  for (const page of pages) {
    page.r.notice.textContent = text;
    page.r.notice.style.color = bad ? "var(--bad)" : "";
  }
}

/** @param {unknown} e */
const reason = (e) =>
  e instanceof ApiError || e instanceof Error ? e.message : String(e);

async function load() {
  try {
    view = await getJson("/api/config/get");
    edits = new Map();
    loaded = true;
    say("");
    render();
  } catch (e) {
    for (const page of pages)
      page.r.body.innerHTML =
        '<div class="empty">The settings could not be read.</div>';
    say(`Could not read config.toml: ${reason(e)}`, true);
  }
}

/** @param {Applied[]} applied */
function showResult(applied) {
  const restart = applied.filter((a) => a.apply === "brain");
  const status = applied.filter((a) => a.apply === "status");
  const live = applied.filter((a) => a.apply === "live");
  const names = (/** @type {Applied[]} */ list) =>
    list.map((a) => a.path.join(".")).join(", ");
  const parts = [
    `Saved ${applied.length} change${applied.length === 1 ? "" : "s"}.`,
  ];
  if (live.length) parts.push(`Applied now: ${names(live)}.`);
  if (restart.length)
    parts.push(
      `Read when the brain starts, so restart it to apply: ${names(restart)}.`,
    );
  if (status.length)
    parts.push(
      `Read by the Admin app at startup (systemctl --user restart deskmate-status): ${names(status)}.`,
    );
  for (const page of pages) {
    page.r.resultText.textContent = parts.join(" ");
    page.r.restart.hidden = restart.length === 0;
    page.r.result.hidden = false;
    page.r.problems.hidden = true;
  }
}

async function save() {
  if (!view) return;
  for (const page of pages) {
    page.r.problems.hidden = true;
    /** @type {HTMLButtonElement} */ (page.r.save).disabled = true;
  }
  say("Saving…");
  try {
    const result = /** @type {SaveResult} */ (
      await write("PUT", "/api/config/save", {
        hash: view.hash,
        changes: changes(),
      })
    );
    view = result.describe;
    edits = new Map();
    render();
    showResult(result.applied);
    say("Saved.");
  } catch (e) {
    for (const page of pages) {
      page.r.problems.hidden = false;
      page.r.problems.textContent = reason(e);
    }
    say("Not saved.", true);
    refresh();
  }
}

async function restore() {
  if (
    !confirm(
      "Put back config.toml as it was before the last save from these pages?",
    )
  )
    return;
  try {
    view = /** @type {View} */ (await write("POST", "/api/config/restore"));
    edits = new Map();
    for (const page of pages) page.r.result.hidden = true;
    render();
    say("Restored the previous version. Restart the brain to apply it.");
  } catch (e) {
    say(reason(e), true);
  }
}

async function restartBrain() {
  if (
    !confirm(
      "Restart the brain now? A reply in progress is cut off and the companion is away for a few seconds.",
    )
  )
    return;
  for (const page of pages) {
    /** @type {HTMLButtonElement} */ (page.r.restart).disabled = true;
  }
  try {
    await write("POST", "/api/config/restart-brain");
    for (const page of pages) {
      page.r.restart.hidden = true;
      page.r.resultText.textContent += " The brain was restarted.";
    }
  } catch (e) {
    say(reason(e), true);
  } finally {
    for (const page of pages) {
      /** @type {HTMLButtonElement} */ (page.r.restart).disabled = false;
    }
  }
}

/** @param {Page} page */
function wire(page) {
  page.r.save.onclick = () => void save();
  page.r.revert.onclick = () => void load();
  page.r.restore.onclick = () => void restore();
  page.r.restart.onclick = () => void restartBrain();
  page.r.filter.addEventListener("input", () => applyFilter(page));
  /** @param {Event} event */
  const fieldOf = (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return null;
    const key = target.dataset.f;
    if (!key || !view) return null;
    for (const g of view.groups)
      for (const f of g.fields) if (id(f) === key) return { f, target };
    return null;
  };
  page.r.body.addEventListener("input", (event) => {
    const hit = fieldOf(event);
    if (!hit) return;
    const { f, target } = hit;
    if (target instanceof HTMLInputElement && target.type === "checkbox") {
      if (target.checked === Boolean(f.value)) edits.delete(id(f));
      else edits.set(id(f), { value: target.checked });
      const label = target.parentElement;
      if (label?.lastChild)
        label.lastChild.textContent = target.checked ? " on" : " off";
    } else if (
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement
    ) {
      onInput(f, target.value);
    }
    refresh();
  });
  page.r.body.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement) || target.dataset.reset === undefined)
      return;
    const key = target.dataset.reset;
    edits.set(key, { unset: true });
    const input = page.r.body.querySelector(`[data-f="${CSS.escape(key)}"]`);
    if (input) /** @type {HTMLElement} */ (input).setAttribute("disabled", "");
    refresh();
  });
}

export function initConfig() {
  for (const root of document.querySelectorAll("[data-cfg-page]")) {
    if (!(root instanceof HTMLElement)) continue;
    root.innerHTML = SKELETON;
    /** @type {Record<string, HTMLElement>} */
    const r = {};
    for (const node of root.querySelectorAll("[data-r]"))
      if (node instanceof HTMLElement) r[node.dataset.r ?? ""] = node;
    const page = {
      sections: (root.dataset.cfgPage ?? "").split(","),
      root,
      path: root.closest("section")?.querySelector("[data-cfg-path]") ?? null,
      r,
    };
    pages.push(page);
    wire(page);
  }
  on("tab", (/** @type {string} */ name) => {
    if (name.startsWith("cfg-") && (!loaded || edits.size === 0)) void load();
  });
}
