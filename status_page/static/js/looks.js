// Looks tab: every version of the companion's appearance with its picture, so a
// version can be named to the companion ("go back to version 12"). Read-only.
import { $, esc } from "./util.js";
import { on } from "./bus.js";

async function load() {
  let data;
  try {
    const response = await fetch("/api/looks");
    data = await response.json();
  } catch {
    $("looksGrid").innerHTML = '<div class="empty">Could not load the looks.</div>';
    return;
  }
  $("looksSummary").textContent = data.looks.length
    ? `${data.looks.length} versions · now version ${data.current}${data.locked ? " · look locked" : ""}`
    : "";
  $("looksLocked").hidden = !data.locked;
  if (!data.looks.length) {
    $("looksGrid").innerHTML = '<div class="empty">No looks built yet.</div>';
    return;
  }
  $("looksGrid").innerHTML = data.looks
    .map((look) => {
      const picture = look.has_picture
        ? `<img loading="lazy" src="/api/looks/${look.version}.png" alt="Version ${look.version}">`
        : "picture pending…";
      const when = look.created_at ? new Date(look.created_at * 1_000).toLocaleString() : "";
      return `<div class="look${look.version === data.current ? " current" : ""}">
        <div class="pic">${picture}</div>
        <div class="meta">
          <div class="ver">Version ${look.version}${look.version === data.current ? '<span class="chip">current</span>' : ""}</div>
          <div class="sum">${esc(look.summary)}</div>
          <div class="muted">${esc(when)}${look.kept ? "" : " · rebuilt on request"}</div>
        </div></div>`;
    })
    .join("");
}

export function initLooks() {
  on("tab", (name) => {
    if (name === "looks") load();
  });
}
