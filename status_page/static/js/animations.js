// Animations tab: which animations each installed body gets from each installed library
// (GET /api/animations), including a body that lacks some of the library's bones. Read-only.
import { $, esc } from "./util.js";
import { on } from "./bus.js";

const SIT = {
  chair: "sits on a chair it brings",
  ground: "sits on the ground",
};

function body(b) {
  const tags = [b.installed ? "installed model" : "built-in"]
    .concat(b.held ? [`held: ${esc(b.held)}`] : [])
    .map((t) => `<span class="chip">${t}</span>`)
    .join("");
  let rows;
  if (b.libraries === null) {
    rows = '<div class="row no">rig never inspected</div>';
  } else if (!Object.keys(b.libraries).length) {
    rows = '<div class="row no">none: it has no rig the libraries fit (a built-in body has its own animations)</div>';
  } else {
    rows = Object.entries(b.libraries)
      .map(([name, fit]) => {
        if (!fit.fits) return `<div class="row no"><span class="lib">${esc(name)}</span> does not fit this rig</div>`;
        const gaps = fit.gaps.length ? ` <span class="muted">· ${fit.gaps.length} bones missing, their tracks are dropped</span>` : "";
        const sit = fit.sit ? ` · ${SIT[fit.sit]}` : "";
        return `<div class="row"><span class="lib">${esc(name)}</span><span>${esc(fit.roles.join(", ") || "no roles")}${sit}${gaps}</span></div>`;
      })
      .join("");
  }
  const credit = b.credit ? `<div class="row muted">credit: ${esc(b.credit)}</div>` : "";
  return `<div class="anim-body"><span class="name">${esc(b.body)}</span> ${tags}${credit}${rows}</div>`;
}

async function load() {
  let data;
  try {
    data = await (await fetch("/api/animations")).json();
  } catch {
    $("animationsBodies").innerHTML = '<div class="empty">Could not load the animations.</div>';
    return;
  }
  $("animationsSummary").textContent = `${data.libraries.length} libraries · ${data.bodies.length} bodies`;
  $("animationsLibraries").innerHTML = data.libraries.length
    ? data.libraries
        .map(
          (l) => `<details class="anim-lib"><summary>${esc(l.name)} <span class="muted">· ${l.clips.length} clips</span></summary>
        <div class="clips">${esc(l.clips.join(", "))}</div></details>`,
        )
        .join("")
    : '<div class="empty">No animation libraries installed.</div>';
  $("animationsBodies").innerHTML = data.bodies.map(body).join("");
}

export function initAnimations() {
  on("tab", (name) => {
    if (name === "animations") load();
  });
}
