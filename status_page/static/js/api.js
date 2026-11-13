// @ts-check
// Talking to this service. Reads are plain GETs; writes (only the Build and Model settings pages) carry the header
// the server requires and turn a refusal into an error with the server's reason in it.

/** A request the server refused or could not make. */
export class ApiError extends Error {
  /**
   * @param {string} message
   * @param {number} status
   */
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

/**
 * A JSON answer. Throws on a status that is not 2xx so a caller can say "unreachable".
 * @template T
 * @param {string} url
 * @returns {Promise<T>}
 */
export async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok)
    throw new ApiError(`HTTP ${response.status}`, response.status);
  return /** @type {T} */ (await response.json());
}

/**
 * The reason in an error body: FastAPI's `detail` is a string, or a list of validation errors.
 * @param {unknown} data
 * @param {number} status
 */
function reason(data, status) {
  const detail = /** @type {{ detail?: unknown } | null} */ (data)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail.map((e) => /** @type {{ msg: string }} */ (e).msg).join("; ");
  return `HTTP ${status}`;
}

/**
 * A write (POST, PUT or DELETE). The server accepts one only from this page: its own Origin, plus the
 * custom header a page on another origin cannot add (status_page/app.py `_write_guard`).
 * @template T
 * @param {"POST" | "PUT" | "DELETE"} method
 * @param {string} url
 * @param {unknown} [body]
 * @returns {Promise<T>}
 */
export async function write(method, url, body) {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-Deskmate-Admin": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  /** @type {unknown} */
  let data = null;
  try {
    data = await response.json();
  } catch {
    /* an empty or non-JSON answer */
  }
  if (!response.ok)
    throw new ApiError(reason(data, response.status), response.status);
  return /** @type {T} */ (data);
}
