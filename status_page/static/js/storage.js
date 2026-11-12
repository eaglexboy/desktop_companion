// @ts-check
// Per-viewer conveniences kept in the browser. Storage can be unavailable (a private window, blocked
// site data), so a failure is "nothing remembered", never an error.

/**
 * @param {string} key
 * @returns {string | null}
 */
export function recall(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * @param {string} key
 * @param {string} value
 */
export function remember(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* nothing is remembered */
  }
}
