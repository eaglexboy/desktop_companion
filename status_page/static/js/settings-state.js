// @ts-check
// What the Model settings page knows (docs/ADR_MODEL_SETTINGS_PAGE.md). The draft is the document
// the user is editing; `saved` is what the brain has. They differ until Save.
import { createStore } from "./store.js";
import { empty } from "./settings-doc.js";

/**
 * @typedef {import("./settings-doc.js").Settings} Settings
 * @typedef {import("./settings-doc.js").Pose} Pose
 */

/**
 * @typedef {object} ListedModel
 * @property {string} id
 * @property {string | null} held  why it is held back, or null
 * @property {boolean} customised
 * @property {string[]} problems
 * @property {string} credit
 */

/**
 * @typedef {object} ChoiceClip
 * @property {string} name
 * @property {string[]} roles  the roles it fills by default
 */
/**
 * @typedef {object} ChoiceLibrary
 * @property {string} name
 * @property {string[]} gaps
 * @property {ChoiceClip[]} clips
 */
/**
 * @typedef {object} Limits
 * @property {number} offset
 * @property {number[]} scale
 * @property {number} rotation
 * @property {number} arm_spread
 * @property {number} emissive_gain
 * @property {number} face_yaw
 */
/**
 * @typedef {object} Choices
 * @property {string[]} roles
 * @property {ChoiceLibrary[]} libraries
 * @property {boolean} has_rig
 * @property {string[]} sockets
 * @property {Record<string, string[]>} items
 * @property {string[]} model_items  "socket/item" for the accessories that are model files
 * @property {Limits} limits
 */
/** @typedef {Record<string, Record<string, number>>} Face */
/**
 * @typedef {object} Credits
 * @property {string} [title]
 * @property {string} [author]
 * @property {string} [source]
 * @property {string} [license]
 * @property {string[]} [notes]
 */
/**
 * One body, from `GET /appearance/model-settings`.
 * @typedef {object} Info
 * @property {string} id
 * @property {string | null} held
 * @property {Settings} settings
 * @property {string[]} problems  why a settings file was ignored
 * @property {string[]} stale  entries that name something no longer there
 * @property {Choices} choices
 * @property {Pose} pose_file  what the older `.pose.json` says
 * @property {Face | null} face
 * @property {Credits | null} credits
 * @property {string[]} licence_asks
 * @property {boolean} in_current_look
 */
/**
 * @typedef {object} Checked
 * @property {string[]} errors
 * @property {string[]} stale
 * @property {boolean} ok
 */

/**
 * @typedef {object} SettingsState
 * @property {ListedModel[] | null} models
 * @property {boolean} unreachable
 * @property {string | null} id  the chosen model
 * @property {Info | null} info
 * @property {Settings} saved
 * @property {Settings} draft
 * @property {Checked | null} checked  the brain's verdict on the draft
 * @property {{ socket: string, item: string } | null} focus  the item whose placement is open
 * @property {boolean} showAllClips
 * @property {Record<string, string | null>} sheetPicks  what the alignment sheet wears, socket by socket: an item, null for nothing; a socket not here has its default
 * @property {boolean} heldOnly  list only the models held back
 * @property {boolean} saving
 */

/** @type {SettingsState} */
const initial = {
  models: null,
  unreachable: false,
  id: null,
  info: null,
  saved: empty(),
  draft: empty(),
  checked: null,
  focus: null,
  showAllClips: false,
  sheetPicks: {},
  heldOnly: false,
  saving: false,
};

export const page = createStore(initial);
export const S = page.state;
