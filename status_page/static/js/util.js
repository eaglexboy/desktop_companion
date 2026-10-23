// Small helpers shared by every module. No state here.

export const $ = (id) => document.getElementById(id);

export const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );

export const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString();

export const fmtClock = (ts, rangeHours) => {
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return rangeHours > 24
    ? d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + time
    : time;
};

export const ago = (s) =>
  s < 90
    ? `${Math.round(s)}s`
    : s < 5400
      ? `${Math.round(s / 60)}m`
      : s < 172800
        ? `${(s / 3600).toFixed(1)}h`
        : `${Math.round(s / 86400)}d`;

export const sec = (ms) =>
  ms == null ? "" : ms >= 1000 ? (ms / 1000).toFixed(1) + " s" : ms + " ms";

export const chips = (list) =>
  list
    .filter(Boolean)
    .map((c) => `<span class="chip">${esc(c)}</span>`)
    .join("");

// Per-viewer convenience storage; it can be unavailable (private window, blocked site data).
export const store = (key, value) => {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) {
    return null;
  }
};

// Fetch JSON from this service; throws on a non-2xx so callers can show "unreachable".
export async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
