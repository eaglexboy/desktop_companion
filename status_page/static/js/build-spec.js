// @ts-check
// Build page: the look spec as the form edits it. Pure functions, no DOM, so they can be
// tested without a browser. The spec has the shape of the brain's LookSpec; the brain
// validates it again, so nothing here is trusted to be right, only to be convenient.

/**
 * @typedef {import("./types.js").Spec} Spec
 * @typedef {import("./types.js").Body} Body
 * @typedef {import("./types.js").BodyKind} BodyKind
 * @typedef {import("./types.js").Socket} Socket
 * @typedef {import("./types.js").PaletteKey} PaletteKey
 * @typedef {import("./types.js").ProportionName} ProportionName
 * @typedef {import("./types.js").Proportions} Proportions
 * @typedef {import("./types.js").Accessories} Accessories
 * @typedef {import("./types.js").Clip} Clip
 * @typedef {import("./types.js").Library} Library
 * @typedef {import("./types.js").Looks} Looks
 * @typedef {import("./types.js").StartGroup} StartGroup
 */

/** @type {PaletteKey[]} */
export const PALETTE_KEYS = [
  "primary",
  "secondary",
  "accent",
  "emissive",
  "hair",
  "hairstyle",
];

/** @type {Record<ProportionName, { min: number, max: number }>} */
export const PROPORTIONS = {
  muscularity: { min: 0, max: 1 },
  femininity: { min: 0, max: 1 },
  height: { min: 0.5, max: 1.5 },
  build: { min: 0.5, max: 1.5 },
};

/** @type {Socket[]} */
export const SOCKETS = [
  "socket_head_top",
  "socket_eyes",
  "socket_neck",
  "socket_chest",
  "socket_waist",
  "socket_hand_R",
  "socket_hand_L",
  "socket_orbit",
];

/** @type {Record<BodyKind, string>} */
export const KIND_LABEL = {
  quaternius: "Quaternius",
  converted: "converted model",
  installed: "installed model",
  procedural: "built-in",
};

/** @type {Record<Socket, string>} */
export const SLOT_LABEL = {
  socket_head_top: "Head top",
  socket_eyes: "Eyes",
  socket_neck: "Neck",
  socket_chest: "Chest",
  socket_waist: "Waist",
  socket_hand_R: "Right hand",
  socket_hand_L: "Left hand",
  socket_orbit: "Orbiting",
};

/** @type {Record<PaletteKey, string>} */
export const COLOR_HINT = {
  primary: "body and eyes",
  secondary: "clothes, soles, shield",
  accent: "trim and accessories",
  emissive: "glow",
  hair: "eyebrows and eyelashes",
  hairstyle: "a hair or beard model",
};

/** The proportions of a body that is neither tall nor short, broad nor slight. */
export function neutralProportions() {
  return /** @type {Proportions} */ (
    Object.fromEntries(
      Object.entries(PROPORTIONS).map(([name, range]) => [
        name,
        range.max > 1 ? 1 : 0.5,
      ]),
    )
  );
}

/**
 * A fresh spec for a body, used when there is no look to start from.
 * @param {Body} body
 * @returns {Spec}
 */
export function defaultSpec(body) {
  return fitToBody(
    {
      base_template: body.id,
      has_face: body.has_face,
      seed: 42,
      proportions: neutralProportions(),
      palette: {
        primary: "#4a90d9",
        secondary: "#ffffff",
        accent: "#f5a623",
        emissive: "#7fd1ff",
        hair: "#ffffff", // white leaves the hair as the model has it
        hairstyle: "#ffffff",
      },
      surface_material: "matte",
      wardrobe: { outfit: "none", footwear: "none" },
      accessories: noAccessories(),
    },
    body,
  );
}

/** @returns {Accessories} */
const noAccessories = () =>
  /** @type {Accessories} */ (
    Object.fromEntries(SOCKETS.map((socket) => [socket, null]))
  );

/**
 * The same spec on `body`: every choice the body does not offer goes back to "none". Colors,
 * material, seed and proportions are kept (proportions only matter on a procedural body, and the
 * brain ignores them elsewhere).
 * @param {Spec} spec
 * @param {Body} body
 * @returns {Spec}
 */
export function fitToBody(spec, body) {
  const { controls } = body;
  /**
   * @param {{ available: boolean, choices: string[] }} control
   * @param {string | null | undefined} value
   */
  const offered = (control, value) =>
    !!value && control.available && control.choices.includes(value);
  const accessories = noAccessories();
  for (const socket of SOCKETS) {
    const wanted = spec.accessories?.[socket];
    accessories[socket] =
      wanted && offered(controls.accessories[socket], wanted) ? wanted : null;
  }
  const { outfit, footwear } = spec.wardrobe ?? {};
  return {
    ...spec,
    base_template: body.id,
    has_face: body.has_face,
    wardrobe: {
      outfit: offered(controls.outfit, outfit) && outfit ? outfit : "none",
      footwear:
        offered(controls.footwear, footwear) && footwear ? footwear : "none",
    },
    accessories,
  };
}

/**
 * What the brain's request wants: the spec, copied.
 * @param {Spec} spec
 * @returns {Spec}
 */
export const toRequest = (spec) => JSON.parse(JSON.stringify(spec));

/**
 * @param {Spec} a
 * @param {Spec} b
 */
export const sameSpec = (a, b) =>
  JSON.stringify(toRequest(a)) === JSON.stringify(toRequest(b));

/**
 * A short line for the preview and apply buttons.
 * @param {Spec} spec
 */
export function summary(spec) {
  return [
    spec.base_template,
    spec.wardrobe?.outfit,
    spec.wardrobe?.footwear,
    ...Object.values(spec.accessories ?? {}),
  ]
    .filter((p) => p && p !== "none")
    .map((p) => String(p).replace(/_/g, " "))
    .join(", ");
}

/**
 * The clip to show first: the idle, else the first.
 * @param {Library[]} libraries
 * @returns {{ library: string, clip: string } | null}
 */
export function defaultClip(libraries) {
  for (const library of libraries) {
    const idle = library.clips.find((c) => c.roles.includes("idle"));
    if (idle) return { library: library.name, clip: idle.name };
  }
  const first = libraries[0]?.clips[0];
  return first ? { library: libraries[0].name, clip: first.name } : null;
}

/**
 * A stored version's spec on `body`, with any field an older spec lacks filled in.
 * @param {Partial<Spec>} stored
 * @param {Body} body
 * @returns {Spec}
 */
export function fromStored(stored, body) {
  const base = defaultSpec(body);
  return fitToBody(
    {
      ...base,
      ...stored,
      proportions: { ...base.proportions, ...stored.proportions },
      palette: { ...base.palette, ...stored.palette },
      wardrobe: { ...base.wardrobe, ...stored.wardrobe },
      accessories: { ...base.accessories, ...stored.accessories },
    },
    body,
  );
}

// --- which colors a look uses -------------------------------------------------------------
// The brain says what each part of a look is colored by (`body.colors`); this adds up what the
// current choices use, so a color nothing uses is disabled and says why.

/**
 * @param {Spec} spec
 * @param {Body} body
 * @returns {Set<string>}
 */
export function usedColors(spec, body) {
  const colors = body.colors;
  // A brain from before the brain said what colors what: assume everything is used.
  if (!colors) return new Set(PALETTE_KEYS);
  const used = new Set(colors.body);
  if (spec.wardrobe.outfit !== "none")
    colors.outfit.forEach((k) => used.add(k));
  if (spec.wardrobe.footwear !== "none")
    colors.footwear.forEach((k) => used.add(k));
  for (const socket of SOCKETS) {
    const item = spec.accessories[socket];
    // an accessory that is a model file has its own colors; only the parametric ones take the palette
    if (item && !(colors.models[socket] ?? []).includes(item))
      (colors.accessories[socket] ?? []).forEach((k) => used.add(k));
    if (item && (colors.hair_models?.[socket] ?? []).includes(item))
      used.add("hairstyle");
  }
  return used;
}

/**
 * Why a color is disabled, or "" when something uses it.
 * @param {PaletteKey} key
 * @param {Spec} spec
 * @param {Body} body
 */
export function colorReason(key, spec, body) {
  const colors = body.colors;
  if (!colors || usedColors(spec, body).has(key)) return "";
  if (key === "hair") return colors.reasons.hair ?? colors.reasons.unused;
  if (key === "hairstyle")
    return colors.reasons.hairstyle ?? colors.reasons.unused;
  return key === "primary" && !colors.body.includes("primary")
    ? colors.reasons.installed
    : colors.reasons.unused;
}

/**
 * Why the surface is disabled, or "" when it is not.
 * @param {Body} body
 */
export const surfaceReason = (body) =>
  !body.colors || body.colors.surface ? "" : body.colors.reasons.surface;

// --- where the accessories go on the figure -------------------------------------------------

/**
 * The sockets this body can carry something on, and those it cannot (with the reason).
 * @param {Body} body
 */
export function slotsFor(body) {
  /** @type {Socket[]} */
  const usable = [];
  /** @type {{ socket: Socket, reason: string | null }[]} */
  const missing = [];
  for (const socket of SOCKETS) {
    const control = body.controls.accessories[socket];
    if (control.available) usable.push(socket);
    else missing.push({ socket, reason: control.reason });
  }
  return { usable, missing };
}

/**
 * The sockets with an accessory chosen that the viewer does not draw (`shown`: the sockets it
 * drew), which can only be judged in Preview.
 * @param {Spec} spec
 * @param {Set<string>} shown
 */
export const previewOnlySockets = (spec, shown) =>
  SOCKETS.filter((socket) => spec.accessories[socket] && !shown.has(socket));

/**
 * Dirty: the form is not what was loaded. A new look on a body has nothing to compare with, so it
 * can always be applied.
 * @param {Spec | null} spec
 * @param {Spec | null} base
 */
export const isDirty = (spec, base) =>
  !!spec && (base === null || !sameSpec(spec, base));

// --- the "Start from" choices ---------------------------------------------------------------

/**
 * Two groups: the looks you have had, and a new look on each body. `looks` is /api/looks.
 * @param {Body[]} bodies
 * @param {Looks | null} looks
 * @param {Record<BodyKind, string>} [kindLabel]
 * @returns {StartGroup[]}
 */
export function startGroups(bodies, looks, kindLabel = KIND_LABEL) {
  const had = (looks?.looks ?? []).map((l) => ({
    value: `v:${l.version}`,
    text: `Version ${l.version}${l.version === looks?.current ? " (current)" : ""} — ${l.summary}`,
  }));
  return [
    { id: "had", label: "A look you have had", items: had },
    {
      id: "new",
      label: "A new look",
      items: bodies.map((b) => ({
        value: `body:${b.id}`,
        text: `A new look on ${b.id} (${kindLabel[b.kind]})`,
      })),
    },
  ].filter((g) => g.items.length);
}

/**
 * The groups reduced to the items that contain every word typed (any order, any case); a group
 * with nothing left is dropped.
 * @param {StartGroup[]} groups
 * @param {string} query
 * @returns {StartGroup[]}
 */
export function filterGroups(groups, query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return groups;
  return groups
    .map((g) => ({
      ...g,
      items: g.items.filter((i) =>
        words.every((w) => i.text.toLowerCase().includes(w)),
      ),
    }))
    .filter((g) => g.items.length);
}

/**
 * The text of the item with this value, or "".
 * @param {StartGroup[]} groups
 * @param {string} value
 */
export const startText = (groups, value) =>
  groups.flatMap((g) => g.items).find((i) => i.value === value)?.text ?? "";
