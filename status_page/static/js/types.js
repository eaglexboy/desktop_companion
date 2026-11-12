// @ts-check
// The shapes the Admin app passes around. These mirror what the brain sends and accepts
// (brain/appearance/spec.py, build_options.py, viewer_files.py and preview.py); a field added there
// is added here, and `npm run check` (status_page/) shows every place that now disagrees.
//
// This file holds only types: there is nothing to run.

/** @typedef {"matte" | "metallic" | "glossy" | "glass" | "emissive_circuit"} Material */
/** @typedef {"primary" | "secondary" | "accent" | "emissive" | "hair" | "hairstyle"} PaletteKey */
/** @typedef {"muscularity" | "femininity" | "height" | "build"} ProportionName */
/**
 * @typedef {"socket_head_top" | "socket_eyes" | "socket_neck" | "socket_chest"
 *   | "socket_waist" | "socket_hand_R" | "socket_hand_L" | "socket_orbit"} Socket
 */

/** @typedef {Record<PaletteKey, string>} Palette */
/** @typedef {Record<ProportionName, number>} Proportions */
/** @typedef {Record<Socket, string | null>} Accessories */
/**
 * @typedef {object} Wardrobe
 * @property {string} outfit
 * @property {string} footwear
 */

/**
 * A look, as the brain's `LookSpec` has it.
 * @typedef {object} Spec
 * @property {string} base_template
 * @property {boolean} has_face
 * @property {number} seed
 * @property {Proportions} proportions
 * @property {Palette} palette
 * @property {Material} surface_material
 * @property {Wardrobe} wardrobe
 * @property {Accessories} accessories
 */

/**
 * One choice the form offers, with the reason when it does not apply to the chosen body.
 * @typedef {object} Control
 * @property {boolean} available
 * @property {string[]} choices
 * @property {string | null} reason
 */

/**
 * Where an accessory that is a model file goes, and how it is skinned.
 * @typedef {object} Placement
 * @property {number[]} offset
 * @property {number} scale
 * @property {number[]} rotation
 * @property {string} skin
 */

/**
 * @typedef {object} ModelPlacements
 * @property {Record<string, Placement>} models  the items that are model files, and where each goes
 */
/** @typedef {Control & ModelPlacements} AccessoryControl */

/**
 * Why a color is disabled, by cause.
 * @typedef {object} ColorReasons
 * @property {string} installed
 * @property {string} unused
 * @property {string} surface
 * @property {string} hair
 * @property {string} hairstyle
 */

/**
 * What colors what, so a color nothing uses can be switched off.
 * @typedef {object} Colors
 * @property {string[]} body
 * @property {boolean} surface
 * @property {string[]} outfit
 * @property {string[]} footwear
 * @property {Record<string, string[]>} accessories
 * @property {Record<string, string[]>} models
 * @property {Record<string, string[]> | undefined} [hair_models]  the model files that are hair or a beard; absent from an older brain
 * @property {ColorReasons} reasons
 */

/** @typedef {"quaternius" | "converted" | "installed" | "procedural"} BodyKind */

/**
 * What each part of a look offers for one body.
 * @typedef {object} BodyControls
 * @property {Control} outfit
 * @property {Control} footwear
 * @property {Control} proportions
 * @property {Record<Socket, AccessoryControl>} accessories
 */

/**
 * One body a look can be built on, from `GET /appearance/options`.
 * @typedef {object} Body
 * @property {string} id
 * @property {BodyKind} kind
 * @property {boolean} held
 * @property {string} credit
 * @property {boolean} has_face
 * @property {string | null} note
 * @property {Colors | undefined} [colors] absent from a brain older than the page
 * @property {BodyControls} controls
 */

/**
 * @typedef {object} Options
 * @property {Material[]} materials
 * @property {Body[]} bodies
 */

/**
 * @typedef {object} LookEntry
 * @property {number} version
 * @property {string} summary
 */
/**
 * @typedef {object} Looks
 * @property {number | null} current
 * @property {boolean} locked
 * @property {LookEntry[]} looks
 */

/**
 * @typedef {object} ClipPart
 * @property {string} clip
 * @property {string[]} roles
 */

/**
 * @typedef {object} Clip
 * @property {string} name
 * @property {string[]} roles
 * @property {ClipPart[]} [part_of]  the clips this one is an ingredient of
 * @property {number} speed
 */
/**
 * @typedef {object} ClipSource
 * @property {string} library
 * @property {string[]} gaps
 */
/** @typedef {Clip & ClipSource} ClipRow */
/**
 * @typedef {object} Library
 * @property {string} name
 * @property {string[]} gaps
 * @property {Clip[]} clips
 */
/**
 * @typedef {object} Animations
 * @property {boolean} model
 * @property {Library[]} libraries
 */

/** @typedef {"queued" | "building" | "rendering" | "ready" | "failed" | "cancelled"} PreviewState */
/**
 * @typedef {object} PreviewInfo
 * @property {string} id
 * @property {PreviewState} state
 * @property {string} summary
 * @property {string | null} error
 * @property {boolean} has_picture
 */

/**
 * @typedef {object} StartItem
 * @property {string} value
 * @property {string} text
 */
/**
 * @typedef {object} StartGroup
 * @property {string} id
 * @property {string} label
 * @property {StartItem[]} items
 */

/**
 * @typedef {object} BrainEventData
 * @property {number} [version]
 * @property {string} [error]
 */

/**
 * An event from the brain's stream, as the page relays it.
 * @typedef {object} BrainEvent
 * @property {number} seq
 * @property {string} type
 * @property {BrainEventData} [data]
 */

export {};
