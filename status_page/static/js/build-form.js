// @ts-check
// The Build page's form: what the chosen body offers, as controls. A control the body cannot use is
// shown switched off with the reason, never hidden (except an accessory socket it cannot carry,
// which is named in one line).
import { all, delegate } from "./dom.js";
import { attrs, html, mount, raw } from "./html.js";
import {
  COLOR_HINT,
  KIND_LABEL,
  PALETTE_KEYS,
  PROPORTIONS,
  SLOT_LABEL,
  colorReason,
  fitToBody,
  previewOnlySockets,
  slotsFor,
  surfaceReason,
} from "./build-spec.js";
import { S, bodyById, currentBody, page } from "./build-state.js";
import { showHair, syncAccessories, updateViewer } from "./build-stage.js";
import { report, ui } from "./build-ui.js";

/**
 * @typedef {import("./types.js").Body} Body
 * @typedef {import("./types.js").Control} Control
 * @typedef {import("./types.js").Options} Options
 * @typedef {import("./types.js").Spec} Spec
 * @typedef {import("./types.js").Socket} Socket
 * @typedef {import("./types.js").PaletteKey} PaletteKey
 * @typedef {import("./types.js").ProportionName} ProportionName
 */

/** @type {Record<string, string>} */
const MATERIAL_LABEL = { emissive_circuit: "glowing circuits" };

/**
 * Where each socket's line meets the figure, in the figure's own drawing units (see `figure`).
 * @type {Record<Socket, { side: "left" | "right", y: number, to: [number, number] }>}
 */
const LEADS = {
  socket_head_top: { side: "left", y: 24, to: [150, 14] },
  socket_neck: { side: "left", y: 88, to: [147, 66] },
  socket_waist: { side: "left", y: 160, to: [138, 168] },
  socket_hand_R: { side: "left", y: 224, to: [98, 206] },
  socket_orbit: { side: "right", y: 24, to: [180, 10] },
  socket_eyes: { side: "right", y: 56, to: [160, 36] },
  socket_chest: { side: "right", y: 120, to: [162, 100] },
  socket_hand_L: { side: "right", y: 224, to: [202, 206] },
};
const LEFT_EDGE = 96;
const RIGHT_EDGE = 204;

/** @param {string} text */
const label = (text) => text.replace(/_/g, " ");

/**
 * A drop-down of what a control offers, "none" first.
 * @param {string} field
 * @param {Control} control
 * @param {string | null | undefined} value
 */
function select(field, control, value) {
  const choices = control.available
    ? ["none", ...control.choices.filter((c) => c !== "none")]
    : ["none"];
  const tag = attrs({
    "data-field": field,
    disabled: !control.available,
    title: control.available ? null : control.reason,
  });
  const options = choices.map(
    (c) =>
      html`<option ${attrs({ value: c, selected: c === (value ?? "none") })}>
        ${label(c)}
      </option>`,
  );
  return html`<select ${tag}>
    ${options}
  </select>`;
}

/**
 * @param {string} name
 * @param {Control} control
 * @param {string} value
 * @param {string} field
 */
function row(name, control, value, field) {
  const why =
    !control.available &&
    control.reason &&
    html`<div class="why">${control.reason}</div>`;
  return html`<div class="field${control.available ? "" : " off"}">
    <label>${name}</label>${select(field, control, value)}${why}
  </div>`;
}

/**
 * @param {PaletteKey} key
 * @param {Spec} spec
 * @param {Body} body
 */
function colorInput(key, spec, body) {
  const why = colorReason(key, spec, body);
  const input = attrs({
    type: "color",
    "data-field": `palette.${key}`,
    value: spec.palette[key],
    disabled: !!why,
  });
  return html`<label
    class="swatch-input${why ? " off" : ""}"
    data-color="${key}"
    ><span class="name">${key}</span><input ${input} /><span class="hint"
      >${why || COLOR_HINT[key]}</span
    ></label
  >`;
}

const SILHOUETTE = raw(`
  <circle class="sil" cx="150" cy="38" r="22"/>
  <path class="sil limb" d="M150 60 L150 74"/>
  <path class="sil" d="M112 78 Q150 70 188 78 L176 190 L124 190 Z"/>
  <path class="sil limb" d="M112 82 L98 204"/>
  <path class="sil limb" d="M188 82 L202 204"/>
  <path class="sil limb" d="M136 190 L132 308"/>
  <path class="sil limb" d="M164 190 L168 308"/>`);

/**
 * The figure the accessories are chosen on: a plain outline with each choice beside the part it
 * goes on. Drawing units only (the viewBox); the picture scales with the page, and where a choice
 * sits is CSS.
 * @param {Spec} spec
 * @param {Body} body
 */
function figure(spec, body) {
  const { usable, missing } = slotsFor(body);
  const controls = body.controls.accessories;
  const leads = usable.map((socket) => {
    const { side, y, to } = LEADS[socket];
    const x = side === "left" ? LEFT_EDGE : RIGHT_EDGE;
    return html`<line
        class="lead"
        x1="${x}"
        y1="${y}"
        x2="${to[0]}"
        y2="${to[1]}"
      /><circle class="dot" cx="${to[0]}" cy="${to[1]}" r="3" />`;
  });
  const slots = usable.map(
    (socket) =>
      html`<label
        class="slot slot-${socket.replace("socket_", "")} ${LEADS[socket].side}"
        ><span>${SLOT_LABEL[socket]}</span>${select(
          `acc.${socket}`,
          controls[socket],
          spec.accessories[socket],
        )}<span class="preview-only" data-preview-only="${socket}" hidden
          >Viewable in Preview only</span
        ></label
      >`,
  );
  const absent =
    missing.length > 0 &&
    html`<div class="why">
      This body cannot carry anything on:
      ${missing.map((m) => SLOT_LABEL[m.socket].toLowerCase()).join(", ")}.
    </div>`;
  return html`<div class="figure">
      <svg class="silhouette" viewBox="0 0 300 320" aria-hidden="true">
        ${SILHOUETTE}${leads}</svg
      >${slots}
    </div>
    ${absent}`;
}

/**
 * @param {ProportionName} name
 * @param {boolean} usable
 * @param {number} value
 */
function proportionRow(name, usable, value) {
  const { min, max } = PROPORTIONS[name];
  const slider = attrs({
    type: "range",
    "data-field": `prop.${name}`,
    min,
    max,
    step: 0.05,
    value,
    disabled: !usable,
  });
  const end = usable
    ? html`<output data-out="prop.${name}">${value.toFixed(2)}</output>`
    : html`<span class="lock">locked</span>`;
  return html`<div class="slider${usable ? "" : " off"}">
    <span>${name}</span><input ${slider} />${end}
  </div>`;
}

/**
 * @param {Spec} spec
 * @param {Body} body
 * @param {Options} options
 */
function formHtml(spec, body, options) {
  const c = body.controls;
  const noSurface = surfaceReason(body);
  const bodies = options.bodies.map(
    (b) =>
      html`<option
        ${attrs({ value: b.id, selected: b.id === spec.base_template })}
      >
        ${b.id} (${KIND_LABEL[b.kind]})
      </option>`,
  );
  const materials = options.materials.map(
    (m) =>
      html`<option
        ${attrs({ value: m, selected: m === spec.surface_material })}
      >
        ${MATERIAL_LABEL[m] ?? label(m)}
      </option>`,
  );
  const proportions = /** @type {ProportionName[]} */ (
    Object.keys(PROPORTIONS)
  ).map((name) =>
    proportionRow(
      name,
      c.proportions.available && c.proportions.choices.includes(name),
      spec.proportions[name],
    ),
  );
  return html` <h3>Body</h3>
    <div class="field">
      <label>Body</label
      ><select data-field="body">
        ${bodies}</select
      >${body.credit && html`<div class="why">${body.credit}</div>`}
    </div>
    <h3>Clothes</h3>
    ${row("Outfit", c.outfit, spec.wardrobe.outfit, "outfit")}
    ${row("Footwear", c.footwear, spec.wardrobe.footwear, "footwear")}
    <h3>Colors</h3>
    ${
      body.note &&
      html`<div class="why">
      ${body.note} Only accessories made from the palette take it.
    </div>`
    }
    <div class="palette">
      ${PALETTE_KEYS.map((key) => colorInput(key, spec, body))}
    </div>
    <div class="field${noSurface ? " off" : ""}">
      <label>Surface</label>
      <select
        ${attrs({
          "data-field": "material",
          disabled: !!noSurface,
          title: noSurface,
        })}
      >
        ${materials}
      </select>
      ${noSurface && html`<div class="why">${noSurface}</div>`}
    </div>
    <h3>Proportions</h3>
    ${
      !c.proportions.available &&
      html`<div class="why locked-note">
      <span class="lock">locked</span> ${c.proportions.reason}
    </div>`
    }
    ${proportions}
    <h3>Accessories</h3>
    ${figure(spec, body)}`;
}

/** Draws the whole form for the current spec (or the invitation to choose one). */
export function renderForm() {
  const { spec, options } = S;
  const body = currentBody();
  mount(
    ui.form,
    spec && options && body
      ? formHtml(spec, body, options)
      : html`<div class="empty">
          Choose where to start: a look you have had, or a new look on a body.
        </div>`,
  );
  page.update(() => {}); // what depends on the form follows
}

// What the form's parts say follows the state, drawn in place (re-drawing the whole form would
// drop the focus).

/** The four colors: each is off, with the reason, while nothing chosen uses it. */
function syncColors() {
  const { spec } = S;
  const body = currentBody();
  if (!spec || !body) return;
  for (const row of all(ui.form, "[data-color]")) {
    const key = /** @type {PaletteKey} */ (row.dataset.color);
    const why = colorReason(key, spec, body);
    row.classList.toggle("off", !!why);
    const input = row.querySelector("input");
    if (input) input.disabled = !!why;
    const hint = row.querySelector(".hint");
    if (hint) hint.textContent = why || COLOR_HINT[key];
  }
}

/**
 * "Viewable in Preview only" under an accessory the viewer cannot draw. Until the viewer has said
 * what it drew (a model is loading) nothing is claimed either way.
 */
function syncPreviewOnly() {
  /** @type {Set<string>} */
  const only = new Set(
    S.spec && S.settled ? previewOnlySockets(S.spec, S.shown) : [],
  );
  for (const note of all(ui.form, "[data-preview-only]"))
    note.hidden = !only.has(note.dataset.previewOnly ?? "");
}

/**
 * A control was changed: update the spec, and whatever shows it.
 * @param {string} field  "body", "outfit", "footwear", "material", "palette.<key>", "prop.<name>" or "acc.<socket>"
 * @param {string} value
 */
export function setField(field, value) {
  const { spec } = S;
  if (!spec) return;
  const [kind, name] = field.split(".");
  if (kind === "body") {
    const body = bodyById(value);
    if (!body) return;
    page.update((s) => {
      s.spec = fitToBody(spec, body);
    });
    renderForm();
    updateViewer().catch(report);
    return;
  }
  page.update(() => {
    if (kind === "outfit") spec.wardrobe.outfit = value;
    else if (kind === "footwear") spec.wardrobe.footwear = value;
    else if (kind === "material")
      spec.surface_material = /** @type {Spec["surface_material"]} */ (value);
    else if (kind === "palette")
      spec.palette[/** @type {PaletteKey} */ (name)] = value;
    else if (kind === "prop")
      spec.proportions[/** @type {ProportionName} */ (name)] = Number(value);
    else if (kind === "acc")
      spec.accessories[/** @type {Socket} */ (name)] =
        value === "none" ? null : value;
  });
  if (kind === "prop") {
    const out = ui.form.querySelector(`[data-out="prop.${name}"]`);
    if (out) out.textContent = Number(value).toFixed(2);
  }
  if (kind === "palette" && (name === "hair" || name === "hairstyle"))
    showHair();
  if (kind === "outfit") updateViewer().catch(report);
  if (kind === "acc") syncAccessories().catch(report);
}

export function initForm() {
  delegate(ui.form, "input", "input[type=range][data-field]", (input) =>
    setField(
      /** @type {string} */ (input.dataset.field),
      /** @type {HTMLInputElement} */ (input).value,
    ),
  );
  delegate(
    ui.form,
    "change",
    "select[data-field], input[data-field]:not([type=range])",
    (control) =>
      setField(
        /** @type {string} */ (control.dataset.field),
        /** @type {HTMLInputElement} */ (control).value,
      ),
  );
  page.subscribe(syncColors);
  page.subscribe(syncPreviewOnly);
}
