// @ts-check
// A model's settings document (blender/model_config.py, format 1) and the edits the Model settings
// page makes to it. Pure functions on plain objects: nothing here touches the page, so the rules
// ("the file holds only what the user changed") live in one place.
//
// Every edit works on a draft and leaves it holding only changes: switching a library back on
// removes its entry, a role set back to Automatic loses its list, and a section with nothing in it
// is dropped, so an untouched model saves no file at all.

/**
 * @typedef {object} Placement
 * @property {number[]} [offset]  metres, in the socket's frame
 * @property {number} [scale]
 * @property {number[]} [rotation]  degrees
 */

/**
 * @typedef {object} Pose
 * @property {number} [face_yaw]
 * @property {Record<string, number>} [roll_bones]
 * @property {number} [emissive_gain]
 * @property {boolean} [relax_arms]
 * @property {number} [arm_spread]
 */

/**
 * @typedef {object} Animations
 * @property {Record<string, boolean>} [libraries]  false: switched off for this model
 * @property {Record<string, boolean>} [clips]  false: switched off for this model
 * @property {Record<string, string[] | null>} [roles]  the clips a role plays; null: nothing
 */

/**
 * @typedef {object} Accessories
 * @property {Record<string, boolean>} [sockets]  false: this model wears nothing there
 * @property {Record<string, string[]>} [items]  the items a socket allows
 * @property {Record<string, Record<string, Placement>>} [placement]  by socket, then item
 */

/**
 * @typedef {object} Settings
 * @property {number} format
 * @property {Animations} [animations]
 * @property {Accessories} [accessories]
 * @property {Pose} [pose]
 */

/**
 * A placement with every field filled in.
 * @typedef {object} FullPlacement
 * @property {number[]} offset
 * @property {number} scale
 * @property {number[]} rotation
 */

export const FORMAT = 1;
export const IDENTITY = { offset: [0, 0, 0], scale: 1, rotation: [0, 0, 0] };

/** @returns {Settings} */
export const empty = () => ({ format: FORMAT });

/**
 * @param {Settings} doc
 * @returns {Settings}
 */
export const clone = (doc) => structuredClone(doc);

/** @param {unknown} value */
const isEmptyObject = (value) =>
  !!value &&
  typeof value === "object" &&
  !Array.isArray(value) &&
  Object.keys(value).length === 0;

/**
 * Drops every empty table, so the document holds only what is changed.
 * @param {Record<string, any>} node
 */
function pruneNode(node) {
  for (const [key, value] of Object.entries(node)) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      pruneNode(value);
      if (isEmptyObject(value)) delete node[key];
    }
  }
}

/**
 * @param {Settings} doc
 * @returns {Settings}
 */
export function prune(doc) {
  pruneNode(doc);
  return doc;
}

/** True when the document changes nothing. @param {Settings} doc */
export const isDefault = (doc) => Object.keys(prune(clone(doc))).length <= 1;

/**
 * Same content, whatever the key order.
 * @param {Settings} a
 * @param {Settings} b
 */
export function same(a, b) {
  return canonical(prune(clone(a))) === canonical(prune(clone(b)));
}

/**
 * @param {unknown} value
 * @returns {string}
 */
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object")
    return `{${Object.entries(value)
      .sort(([x], [y]) => (x < y ? -1 : x > y ? 1 : 0))
      .map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`)
      .join(",")}}`;
  return JSON.stringify(value);
}

/**
 * The table `path` names inside `doc`, made if it is not there.
 * @param {Settings} doc
 * @param {string[]} path
 * @returns {Record<string, any>}
 */
function table(doc, path) {
  /** @type {Record<string, any>} */
  let node = doc;
  for (const key of path) node = node[key] ??= {};
  return node;
}

// --- animations ------------------------------------------------------------------------------

/**
 * A library on (the default) or switched off for this body.
 * @param {Settings} doc
 * @param {string} name
 * @param {boolean} on
 */
export function setLibrary(doc, name, on) {
  const libraries = table(doc, ["animations", "libraries"]);
  if (on) delete libraries[name];
  else libraries[name] = false;
  return prune(doc);
}

/**
 * A clip on (the default) or switched off.
 * @param {Settings} doc
 * @param {string} name
 * @param {boolean} on
 */
export function setClip(doc, name, on) {
  const clips = table(doc, ["animations", "clips"]);
  if (on) delete clips[name];
  else clips[name] = false;
  return prune(doc);
}

/**
 * @param {Settings} doc
 * @param {string} name
 */
export const libraryOn = (doc, name) =>
  doc.animations?.libraries?.[name] !== false;

/**
 * @param {Settings} doc
 * @param {string} name
 */
export const clipOn = (doc, name) => doc.animations?.clips?.[name] !== false;

/** @typedef {"auto" | "chosen" | "off"} RoleMode */

/**
 * How a role is filled: left to the rules, by the clips the user chose, or not at all.
 * @param {Settings} doc
 * @param {string} role
 * @returns {RoleMode}
 */
export function roleMode(doc, role) {
  const roles = doc.animations?.roles;
  if (!roles || !(role in roles)) return "auto";
  return roles[role] === null ? "off" : "chosen";
}

/**
 * @param {Settings} doc
 * @param {string} role
 * @returns {string[]}
 */
export const roleClips = (doc, role) => doc.animations?.roles?.[role] ?? [];

/**
 * Switch a role between automatic, chosen clips and off. Choosing starts from `seed` (the clips the
 * rules would use), so it can be tuned rather than built from nothing.
 * @param {Settings} doc
 * @param {string} role
 * @param {RoleMode} mode
 * @param {string[]} [seed]
 */
export function setRoleMode(doc, role, mode, seed = []) {
  const roles = table(doc, ["animations", "roles"]);
  if (mode === "auto") delete roles[role];
  else if (mode === "off") roles[role] = null;
  else
    roles[role] = roleClips(doc, role).length
      ? roleClips(doc, role)
      : [...seed];
  return prune(doc);
}

/**
 * Replace the clip at `index` of a chosen role; an index past the end adds one.
 * @param {Settings} doc
 * @param {string} role
 * @param {number} index
 * @param {string} clip
 */
export function setRoleClip(doc, role, index, clip) {
  const roles = table(doc, ["animations", "roles"]);
  const list = Array.isArray(roles[role]) ? roles[role] : [];
  list[index] = clip;
  roles[role] = list;
  return doc;
}

/**
 * @param {Settings} doc
 * @param {string} role
 * @param {number} index
 */
export function removeRoleClip(doc, role, index) {
  const list = doc.animations?.roles?.[role];
  if (Array.isArray(list)) list.splice(index, 1);
  return doc;
}

/**
 * Move a chosen clip one place earlier (the first is the role's own).
 * @param {Settings} doc
 * @param {string} role
 * @param {number} index
 */
export function moveRoleClipUp(doc, role, index) {
  const list = doc.animations?.roles?.[role];
  if (Array.isArray(list) && index > 0)
    [list[index - 1], list[index]] = [list[index], list[index - 1]];
  return doc;
}

// --- accessories -----------------------------------------------------------------------------

/**
 * @param {Settings} doc
 * @param {string} socket
 */
export const socketOn = (doc, socket) =>
  doc.accessories?.sockets?.[socket] !== false;

/**
 * @param {Settings} doc
 * @param {string} socket
 * @param {boolean} on
 */
export function setSocket(doc, socket, on) {
  const sockets = table(doc, ["accessories", "sockets"]);
  if (on) delete sockets[socket];
  else sockets[socket] = false;
  return prune(doc);
}

/**
 * Whether `item` may be worn on `socket`: everything is, until the socket has a list.
 * @param {Settings} doc
 * @param {string} socket
 * @param {string} item
 */
export function itemOn(doc, socket, item) {
  const list = doc.accessories?.items?.[socket];
  return !list || list.includes(item);
}

/**
 * Allow or forbid one item. The list is written only while it differs from "everything".
 * @param {Settings} doc
 * @param {string} socket
 * @param {string} item
 * @param {boolean} on
 * @param {string[]} all  every item offered on the socket
 */
export function setItem(doc, socket, item, on, all) {
  const items = table(doc, ["accessories", "items"]);
  const allowed = all.filter((i) => (i === item ? on : itemOn(doc, socket, i)));
  if (allowed.length === all.length) delete items[socket];
  else items[socket] = allowed;
  return prune(doc);
}

/**
 * The placement of an item for this body, with the identity filled in.
 * @param {Settings} doc
 * @param {string} socket
 * @param {string} item
 */
export function placementOf(doc, socket, item) {
  const p = doc.accessories?.placement?.[socket]?.[item] ?? {};
  return {
    offset: [...(p.offset ?? IDENTITY.offset)],
    scale: p.scale ?? IDENTITY.scale,
    rotation: [...(p.rotation ?? IDENTITY.rotation)],
  };
}

/**
 * Set the placement of an item; the identity removes the entry.
 * @param {Settings} doc
 * @param {string} socket
 * @param {string} item
 * @param {FullPlacement} value
 */
export function setPlacement(doc, socket, item, value) {
  const bySocket = table(doc, ["accessories", "placement", socket]);
  /** @type {Placement} */
  const entry = {};
  if (value.offset.some((v) => v !== 0)) entry.offset = value.offset;
  if (value.scale !== 1) entry.scale = value.scale;
  if (value.rotation.some((v) => v !== 0)) entry.rotation = value.rotation;
  if (Object.keys(entry).length) bySocket[item] = entry;
  else delete bySocket[item];
  return prune(doc);
}

// --- pose ------------------------------------------------------------------------------------

/**
 * Set one pose value; null (or an empty field) goes back to automatic.
 * @param {Settings} doc
 * @param {"face_yaw" | "emissive_gain" | "arm_spread" | "relax_arms"} key
 * @param {number | boolean | null} value
 */
export function setPose(doc, key, value) {
  const pose = /** @type {Record<string, any>} */ (table(doc, ["pose"]));
  if (value === null || (key === "relax_arms" && value === true))
    delete pose[key];
  else pose[key] = value;
  return prune(doc);
}

/**
 * Turn a bone about its own length by `degrees`; null removes it.
 * @param {Settings} doc
 * @param {string} bone
 * @param {number | null} degrees
 */
export function setRoll(doc, bone, degrees) {
  const rolls = table(doc, ["pose", "roll_bones"]);
  if (degrees === null) delete rolls[bone];
  else rolls[bone] = degrees;
  return prune(doc);
}

// --- reading a field -------------------------------------------------------------------------

/**
 * A number from a text field: null for an empty field, NaN for text that is not a number.
 * @param {string} text
 */
export function parseNumber(text) {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  return /^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(trimmed)
    ? Number(trimmed)
    : Number.NaN;
}
