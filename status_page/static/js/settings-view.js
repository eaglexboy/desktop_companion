// @ts-check
// The Model settings page's forms: pure functions from what the page knows to markup. Controls
// carry `data-act` and the names they act on; settings.js reads those, so nothing here has a
// handler of its own.
import { attrs, html } from "./html.js";
import * as doc from "./settings-doc.js";

/**
 * @typedef {import("./settings-state.js").Info} Info
 * @typedef {import("./settings-state.js").Checked} Checked
 * @typedef {import("./settings-doc.js").Settings} Settings
 */

const ROLE_MODES = /** @type {const} */ ([
  ["auto", "Automatic"],
  ["chosen", "These clips"],
  ["off", "Off for this model"],
]);

/**
 * Every clip a role may use now: in a library that is on, and not switched off.
 * @param {Info} info
 * @param {Settings} draft
 */
export function usableClips(info, draft) {
  return info.choices.libraries
    .filter((lib) => doc.libraryOn(draft, lib.name))
    .flatMap((lib) => lib.clips)
    .filter((clip) => doc.clipOn(draft, clip.name))
    .map((clip) => clip.name);
}

/**
 * The clips the rules would use for a role (the first is its own, the rest its alternatives).
 * @param {Info} info
 * @param {Settings} draft
 * @param {string} role
 */
export function defaultsFor(info, draft, role) {
  return info.choices.libraries
    .filter((lib) => doc.libraryOn(draft, lib.name))
    .flatMap((lib) => lib.clips)
    .filter((clip) => doc.clipOn(draft, clip.name) && clip.roles.includes(role))
    .map((clip) => clip.name);
}

/**
 * The library a clip is played from.
 * @param {Info} info
 * @param {string} clip
 */
export function libraryOfClip(info, clip) {
  return (
    info.choices.libraries.find((lib) => lib.clips.some((c) => c.name === clip))
      ?.name ?? null
  );
}

/**
 * @param {Info} info
 * @param {Settings} draft
 * @param {boolean} showAll
 */
export function animations(info, draft, showAll) {
  const { choices } = info;
  if (!choices.has_rig) {
    return html`<p class="note">
      No animation library fits this model's rig, so there are no roles to
      choose for it: it plays the motion it is built with. Libraries are
      installed with <code>models add-animations</code>.
    </p>`;
  }
  const usable = usableClips(info, draft);
  const libraries = choices.libraries.map((lib) => {
    const on = doc.libraryOn(draft, lib.name);
    const tag = attrs({
      type: "checkbox",
      "data-act": "library",
      "data-name": lib.name,
      checked: on,
    });
    return html`<div class="row">
      <label class="check"><input ${tag} /> <span class="name">${lib.name}</span></label>
      <span class="note"
        >${lib.clips.length} animations${
          lib.gaps.length
            ? `, without the tracks of ${lib.gaps.length} bones this model lacks`
            : ""
        }</span
      >
    </div>`;
  });
  const roles = choices.roles.map((role) => {
    const mode = doc.roleMode(draft, role);
    const defaults = defaultsFor(info, draft, role);
    const modeTag = attrs({ "data-act": "role-mode", "data-role": role });
    const options = ROLE_MODES.map(
      ([value, label]) =>
        html`<option ${attrs({ value, selected: value === mode })}>${label}</option>`,
    );
    const customised = mode !== "auto";
    const summary =
      mode === "auto"
        ? defaults.length
          ? `Automatic: ${defaults.join(", ")}`
          : "Nothing fills it for this model"
        : mode === "off"
          ? defaults.length
            ? `Would be: ${defaults.join(", ")}`
            : ""
          : `Automatic would be: ${defaults.join(", ") || "nothing"}`;
    const chosen =
      mode === "chosen"
        ? html`<div class="role-clips">
            ${doc.roleClips(draft, role).map((clip, i, list) => {
              const present = usable.includes(clip);
              const pick = attrs({
                "data-act": "role-clip",
                "data-role": role,
                "data-i": i,
              });
              return html`<div class="clip-row">
                <select ${pick} aria-label="Animation ${i + 1} for ${role}">
                  ${present ? "" : html`<option ${attrs({ value: clip, selected: true })}>${clip} (no longer available)</option>`}
                  ${usable.map(
                    (name) =>
                      html`<option ${attrs({ value: name, selected: name === clip })}>${name}</option>`,
                  )}
                </select>
                <button ${attrs({ type: "button", "data-act": "play", "data-clip": clip, title: "Play it on the model", disabled: !present })}>Play</button>
                <button ${attrs({ type: "button", "data-act": "clip-up", "data-role": role, "data-i": i, title: "Earlier: the first is the role's own", disabled: i === 0 })}>Earlier</button>
                <button ${attrs({ type: "button", "data-act": "clip-del", "data-role": role, "data-i": i })}>${list.length === 1 ? "Back to automatic" : "Remove"}</button>
                <span class="note">${i === 0 ? "the role's own" : "an alternative"}</span>
              </div>`;
            })}
          </div>`
        : "";
    return html`<div class="row${customised || defaults.length ? "" : " dim"}">
      <span class="name">${role}</span>
      <select ${modeTag} aria-label="How ${role} is filled">${options}</select>
      <select ${attrs({ "data-act": "role-add", "data-role": role, disabled: !usable.length, "aria-label": `Add an animation to ${role}` })}>
        <option value="">Add animation…</option>
        ${usable.map((name) => html`<option ${attrs({ value: name })}>${name}</option>`)}
      </select>
      <span class="note">${summary}</span>
      ${chosen}
    </div>`;
  });
  const clips = choices.libraries.flatMap((lib) =>
    lib.clips.map((clip) => ({ lib: lib.name, ...clip })),
  );
  const off = clips.filter((c) => !doc.clipOn(draft, c.name)).length;
  const clipRows = (
    showAll
      ? clips
      : clips.filter((c) => c.roles.length || !doc.clipOn(draft, c.name))
  ).map((clip) => {
    const tag = attrs({
      type: "checkbox",
      "data-act": "clip",
      "data-name": clip.name,
      checked: doc.clipOn(draft, clip.name),
    });
    return html`<div class="row">
      <label class="check"><input ${tag} /> <span class="name">${clip.name}</span></label>
      <span class="note">${clip.roles.length ? clip.roles.join(", ") : "no role"} · ${clip.lib}</span>
      <select ${attrs({ "data-act": "clip-role", "data-clip": clip.name, disabled: !doc.clipOn(draft, clip.name), "aria-label": `Use ${clip.name} for a role` })}>
        <option value="">Use for role…</option>
        ${choices.roles.map((role) => html`<option ${attrs({ value: role })}>${role}</option>`)}
      </select>
      <button ${attrs({ type: "button", "data-act": "play", "data-clip": clip.name })}>Play</button>
    </div>`;
  });
  return html`
    <h4>Libraries this model may use</h4>
    ${libraries}
    <h4>Roles</h4>
    <p class="note">
      A role is something the companion does (idle, talk, greet…). Use
      <b>Add animation…</b> on a role to pick the animations it plays: the
      first is its own and the others are alternatives, chosen from at random,
      and you can reorder, play or remove them. <b>Automatic</b> leaves the role to
      the rules; <b>Off</b> gives the model nothing for it. A role nothing fills
      yet has no animation until you add one.
    </p>
    ${roles}
    <h4>Animations switched off for this model (${off})</h4>
    <p class="note">Some animations do not suit a model (a different skeleton, or a pose its shape cannot do).
      <b>Play</b> one to watch it on the model; if it looks wrong, use <b>Doesn't work on this model</b>
      under the viewer, or untick it here. A switched-off animation is used for no role of this model.
      <b>Show every animation</b> lists them all, not only those that fill a role.</p>
    ${clipRows}
    <button ${attrs({ type: "button", "data-act": "toggle-all-clips" })}>${showAll ? "Only animations that fill a role" : "Show every animation"}</button>
  `;
}

/**
 * @param {Info} info
 * @param {Settings} draft
 * @param {{ socket: string, item: string } | null} focus
 */
export function accessories(info, draft, focus) {
  const { choices } = info;
  if (!choices.sockets.length)
    return html`<p class="note">This model carries no accessories.</p>`;
  const rows = choices.sockets.map((socket) => {
    const all = choices.items[socket] ?? [];
    const socketTag = attrs({
      type: "checkbox",
      "data-act": "socket",
      "data-socket": socket,
      checked: doc.socketOn(draft, socket),
    });
    const on = doc.socketOn(draft, socket);
    const items = all.map((item) => {
      const itemTag = attrs({
        type: "checkbox",
        "data-act": "item",
        "data-socket": socket,
        "data-item": item,
        checked: doc.itemOn(draft, socket, item),
        disabled: !on,
      });
      const placed = !!draft.accessories?.placement?.[socket]?.[item];
      const isFocus = focus?.socket === socket && focus.item === item;
      return html`<span class="check"
        ><label class="check"><input ${itemTag} /> ${item}</label>
        <button ${attrs({ type: "button", "data-act": "place", "data-socket": socket, "data-item": item, "aria-expanded": isFocus, title: "Where this item sits on this model" })}>${placed ? "Placed" : "Place"}${isFocus ? " ▾" : ""}</button></span
      >`;
    });
    const panel =
      focus?.socket === socket && all.includes(focus.item)
        ? placement(info, draft, focus.socket, focus.item)
        : "";
    return html`<div class="row${on ? "" : " dim"}">
      <label class="check"><input ${socketTag} /> <span class="name">${socket.replace("socket_", "")}</span></label>
      <span class="items">${items}</span>
      ${panel}
    </div>`;
  });
  return html`
    <p class="note">
      Which sockets this model may wear something on, and which items. Place
      adjusts where an item sits <i>on this model</i>, on top of the item's own
      placement; items that are model files are drawn live in the viewer, the
      others are judged on the alignment sheet.
    </p>
    ${rows}
  `;
}

/**
 * @param {Info} info
 * @param {Settings} draft
 * @param {string} socket
 * @param {string} item
 */
function placement(info, draft, socket, item) {
  const p = doc.placementOf(draft, socket, item);
  const { limits } = info.choices;
  const field = (
    /** @type {string} */ label,
    /** @type {string} */ name,
    /** @type {number} */ value,
    /** @type {number} */ step,
    /** @type {number} */ lo,
    /** @type {number} */ hi,
  ) =>
    html`<label>${label}<input ${attrs({ type: "number", "data-act": "place-field", "data-field": name, value, step, min: lo, max: hi })} /></label>`;
  const o = limits.offset;
  return html`<div class="placement" data-socket="${socket}" data-item="${item}">
    ${field("Offset x (m)", "ox", p.offset[0], 0.01, -o, o)}
    ${field("Offset y (m)", "oy", p.offset[1], 0.01, -o, o)}
    ${field("Offset z (m)", "oz", p.offset[2], 0.01, -o, o)}
    ${field("Scale", "scale", p.scale, 0.05, limits.scale[0], limits.scale[1])}
    ${field("Rotation x (°)", "rx", p.rotation[0], 5, -limits.rotation, limits.rotation)}
    ${field("Rotation y (°)", "ry", p.rotation[1], 5, -limits.rotation, limits.rotation)}
    ${field("Rotation z (°)", "rz", p.rotation[2], 5, -limits.rotation, limits.rotation)}
    <button ${attrs({ type: "button", "data-act": "place-reset", "data-socket": socket, "data-item": item })}>Reset</button>
    <span class="note" style="grid-column: 1 / -1">${info.choices.model_items.includes(`${socket}/${item}`) ? "Drawn live in the viewer (a rotation shows in the alignment sheet)." : "Built only inside Blender: use the alignment sheet to judge it."} Offsets are in the socket's frame, as the build uses it.</span>
  </div>`;
}

/**
 * @param {Info} info
 * @param {Settings} draft
 */
export function pose(info, draft) {
  const p = draft.pose ?? {};
  const file = info.pose_file;
  const { limits } = info.choices;
  const hint = (/** @type {unknown} */ v) =>
    v === undefined ? "automatic" : `${v} (from pose.json)`;
  const num = (
    /** @type {string} */ key,
    /** @type {number | undefined} */ value,
    /** @type {number} */ lo,
    /** @type {number} */ hi,
    /** @type {unknown} */ fallback,
  ) =>
    html`<input ${attrs({ type: "number", "data-act": "pose", "data-key": key, value: value ?? "", placeholder: hint(fallback), min: lo, max: hi, step: "any" })} />`;
  const rolls = Object.entries(p.roll_bones ?? {}).map(
    ([bone, degrees]) => html`<div class="row">
      <span class="name">${bone}</span>
      <input ${attrs({ type: "number", class: "num", "data-act": "roll", "data-bone": bone, value: degrees, min: -limits.rotation, max: limits.rotation, "aria-label": `Degrees to turn ${bone}` })} />
      <span class="note">degrees about its own length</span>
      <button ${attrs({ type: "button", "data-act": "roll-del", "data-bone": bone })}>Remove</button>
    </div>`,
  );
  const fileRolls = Object.keys(file.roll_bones ?? {});
  const arms = attrs({
    type: "checkbox",
    "data-act": "relax",
    checked: p.relax_arms !== false,
  });
  const face = info.face;
  const faceRows = face
    ? ["eye_l", "eye_r", "mouth"].map(
        (part) => html`<div class="row"><span class="name">${part}</span>
        ${["x", "z", "w", "h"].map(
          (k) => html`<label class="check">${k}
          <input ${attrs({ type: "number", class: "num", "data-act": "face", "data-part": part, "data-k": k, value: face[part]?.[k], min: 0, max: 1, step: 0.01 })} /></label>`,
        )}
      </div>`,
      )
    : "";
  return html`
    <div class="field-grid">
      <label>Turn to face the front (degrees)${num("face_yaw", p.face_yaw, -limits.face_yaw, limits.face_yaw, file.face_yaw)}</label>
      <label>Glow multiplier${num("emissive_gain", p.emissive_gain, 0.01, limits.emissive_gain, file.emissive_gain)}</label>
      <label>Hold the upper arms out (degrees)${num("arm_spread", p.arm_spread, 0, limits.arm_spread, undefined)}</label>
      <label class="check"><input ${arms} /> Relax arms held out, when no animation library is worn</label>
    </div>
    <h4>Bones to turn</h4>
    <p class="note">A hand modelled with its fingers turned outward makes an arm bend the wrong way; turning that bone fixes it.${fileRolls.length ? ` pose.json also turns: ${fileRolls.join(", ")}.` : ""}</p>
    ${rolls}
    <div class="row">
      <input ${attrs({ type: "text", id: "msRollBone", placeholder: "bone name", "aria-label": "Bone to turn" })} />
      <input ${attrs({ type: "number", id: "msRollDeg", class: "num", value: 90, "aria-label": "Degrees" })} />
      <button ${attrs({ type: "button", "data-act": "roll-add" })}>Add</button>
    </div>
    <h4>Face landmarks</h4>
    ${
      face
        ? html`<p class="note">Where the eyes and mouth are, as fractions of the model's box (measured from the model: change them only to correct a bad measurement). Saved on their own.</p>
        ${faceRows}
        <div class="actions"><button ${attrs({ type: "button", "data-act": "face-save" })}>Save landmarks</button></div>`
        : html`<p class="note">This model has no face landmarks (it has face shapes of its own, or none).</p>`
    }
  `;
}

/** @param {Info} info */
export function availability(info) {
  const c = info.credits ?? {};
  const isCustom = info.id.startsWith("custom:");
  const hold = !isCustom
    ? html`<p class="note">A standard model cannot be held back.</p>`
    : info.held
      ? html`<div class="row"><span>Held back: <b>${info.held}</b>. It is neither offered to the designer nor accepted for a look.</span>
          <button ${attrs({ type: "button", "data-act": "release" })}>Release</button></div>`
      : html`<div class="row">
          <input ${attrs({ type: "text", id: "msHoldReason", placeholder: "why (a short line)", maxlength: 400, "aria-label": "Reason to hold" })} />
          <button ${attrs({ type: "button", "data-act": "hold" })}>Hold back</button>
          <span class="note">until its licence is checked, say</span></div>`;
  const text = (/** @type {string} */ key, /** @type {string} */ label) =>
    html`<label>${label}<input ${attrs({ type: "text", "data-act": "credit", "data-key": key, value: /** @type {Record<string, any>} */ (c)[key] ?? "", maxlength: 400 })} /></label>`;
  return html`
    <h4>Availability</h4>
    ${hold}
    <h4>Credit and licence</h4>
    ${info.licence_asks.length ? html`<p class="note">The licence asks you to: ${info.licence_asks.join("; ")}.</p>` : ""}
    <div class="field-grid">
      ${text("title", "Title")} ${text("author", "Author")} ${text("source", "Source")} ${text("license", "Licence")}
      <label class="span">Notes (one per line)<textarea ${attrs({ "data-act": "credit-notes" })}>${(c.notes ?? []).join("\n")}</textarea></label>
    </div>
    <div class="actions"><button ${attrs({ type: "button", "data-act": "credit-save" })}>Save credit</button></div>
  `;
}

/**
 * @param {Info} info
 * @param {Checked | null} checked
 * @param {string[]} stale
 */
export function problems(info, checked, stale) {
  const errors = checked?.errors ?? [];
  const parts = [];
  if (info.problems.length)
    parts.push(
      html`<div class="problems warn">The saved settings file was ignored: ${info.problems.join("; ")}</div>`,
    );
  if (errors.length)
    parts.push(
      html`<div class="problems bad"><b>Cannot be saved yet</b><ul>${errors.map((e) => html`<li>${e}</li>`)}</ul></div>`,
    );
  if (stale.length)
    parts.push(html`<div class="problems warn"><b>No longer available</b> (ignored by builds)<ul>${stale.map((e) => html`<li>${e}</li>`)}</ul>
      <button ${attrs({ type: "button", "data-act": "remove-stale" })}>Remove these entries</button></div>`);
  return html`${parts}`;
}

const NOTHING = "__nothing__";

/**
 * What the alignment sheet renders: one choice per socket, "Default" being the item the build would
 * pick (the one with a placement, else the first), so that an item can be checked on its own.
 * @param {Info} info
 * @param {Settings} draft
 * @param {Record<string, string | null>} picks
 * @param {{ socket: string, item: string } | null} focus
 */
export function sheetPicker(info, draft, picks, focus) {
  const { choices } = info;
  const sockets = choices.sockets.filter(
    (socket) => doc.socketOn(draft, socket) && choices.items[socket]?.length,
  );
  if (!sockets.length) return html``;
  const rows = sockets.map((socket) => {
    const pick = socket in picks ? picks[socket] : undefined;
    const value = pick === undefined ? "" : pick === null ? NOTHING : pick;
    const options = [
      ["", "Default"],
      [NOTHING, "Nothing"],
      ...(choices.items[socket] ?? []).map((item) => [item, item]),
    ];
    return html`<label class="row">
      <span class="name">${socket.replace("socket_", "")}</span>
      <select ${attrs({ "data-act": "sheet-pick", "data-socket": socket, "aria-label": `What the sheet wears on ${socket}` })}>
        ${options.map(([v, text]) => html`<option ${attrs({ value: v, selected: v === value })}>${text}</option>`)}
      </select>
    </label>`;
  });
  const only =
    focus && sockets.includes(focus.socket)
      ? html`<button ${attrs({ type: "button", "data-act": "sheet-only", title: "Render only the item whose Place panel is open" })}>Only ${focus.item}</button>`
      : "";
  return html`
    <p class="note">What the sheet renders (everything by default):</p>
    ${rows}
    <div class="actions">
      ${only}
      <button ${attrs({ type: "button", "data-act": "sheet-all" })}>Everything</button>
      <button ${attrs({ type: "button", "data-act": "sheet-none" })}>Nothing</button>
    </div>`;
}

export { NOTHING };
