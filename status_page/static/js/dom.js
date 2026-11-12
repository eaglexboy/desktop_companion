// @ts-check
// Typed ways to find elements, so a missing id is an error with its name in it rather than a
// "cannot read properties of null" somewhere else.

/**
 * The element with this id, checked to be what the caller says it is.
 * @template {HTMLElement} [T=HTMLElement]
 * @param {string} id
 * @param {new (...args: never[]) => T} [type]
 * @returns {T}
 */
export function el(id, type) {
  const found = document.getElementById(id);
  if (!found) throw new Error(`#${id} is missing from index.html`);
  if (type && !(found instanceof type))
    throw new Error(`#${id} is not a ${type.name}`);
  return /** @type {T} */ (found);
}

/**
 * Runs `handler` for events from inside `root` whose target is, or is inside, an element that
 * matches `selector` (one listener for a list whose rows are drawn again and again).
 * @template {Element} [T=HTMLElement]
 * @param {Element | Document} root
 * @param {keyof HTMLElementEventMap} type
 * @param {string} selector
 * @param {(match: T, event: Event) => void} handler
 */
export function delegate(root, type, selector, handler) {
  root.addEventListener(type, (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const match = target?.closest(selector);
    if (match && root.contains(match)) handler(/** @type {T} */ (match), event);
  });
}

/**
 * Every element under `root` that matches `selector`.
 * @param {ParentNode} root
 * @param {string} selector
 * @returns {HTMLElement[]}
 */
export const all = (root, selector) =>
  /** @type {HTMLElement[]} */ ([...root.querySelectorAll(selector)]);
