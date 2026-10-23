// Live mood and trait bars: seeded from each overview poll, then kept current by
// personality.* events from the brain's stream.
import { $, esc } from "./util.js";
import { on } from "./bus.js";
import { FEELING_COLORS, feeling } from "./feelings.js";

// Shared with the timeline (its legend shows the latest values).
export const live = { mood: {}, traits: {}, moodDelta: {}, traitDelta: {} };

function bars(el, values, deltas) {
  el.innerHTML = Object.entries(values)
    .map(([k, v]) => {
      const d = deltas[k];
      const delta =
        d && Math.abs(d) >= 0.05
          ? `<span class="delta ${d > 0 ? "up" : "down"}">${d > 0 ? "▲" : "▼"}${Math.abs(d).toFixed(1)}</span>`
          : "<span></span>";
      return `<span>${esc(k)}</span><span class="bar"><i style="width:${Math.max(0, Math.min(100, v))}%"></i></span><span class="muted">${Math.round(v)}</span>${delta}`;
    })
    .join("");
}

function draw() {
  bars($("mood"), live.mood, live.moodDelta);
  bars($("traits"), live.traits, live.traitDelta);
  const f = Object.keys(live.mood).length ? feeling(live.mood) : "—";
  $("feeling").innerHTML =
    `<span class="sw dot" style="background:${FEELING_COLORS[f] || "var(--muted)"}"></span>${esc(f)}`;
}

function applyEvent(ev) {
  const kind = ev.type === "personality.mood" ? "mood" : "traits";
  const d = ev.data;
  const delta = {};
  for (const [k, v] of Object.entries(d.values))
    delta[k] = v - (live[kind][k] ?? v);
  live[kind] = { ...d.values };
  if (d.cause !== "decay" && d.cause !== "startup") {
    live[kind === "mood" ? "moodDelta" : "traitDelta"] =
      d.deltas && Object.keys(d.deltas).length ? d.deltas : delta;
  }
  draw();
}

export function initPersonality() {
  on("overview", (d) => {
    const h = d.brain.health;
    if (h.error) return;
    live.mood = { ...h.mood };
    live.traits = { ...h.traits };
    draw();
  });
  on("stream", (ev) => {
    if (ev.type === "personality.mood" || ev.type === "personality.traits")
      applyEvent(ev);
  });
}
