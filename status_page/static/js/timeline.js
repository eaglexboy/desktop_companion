// Mood timeline tab: step-line chart of mood (or up to three traits) over a
// selectable range, hover readout, table view, and a strip of derived feelings.
// History comes from /api/history; live personality.* events extend it.
import { $, esc, fmtClock, getJSON } from "./util.js";
import { on } from "./bus.js";
import { FEELING_COLORS, feeling } from "./feelings.js";
import { live } from "./personality.js";
import { valueAt } from "./series.js";

const SERIES_VARS = ["--s1", "--s2", "--s3"]; // validated categorical slots (see css/tokens.css)
const MOOD_NAMES = ["valence", "energy", "stress"];
const MAX_PICK = 3;
const MARGIN = { l: 34, r: 56, t: 10, b: 44 };
const HEIGHT = 300;
const FEELING_SAMPLES = 160;

const tl = { data: null, range: 24, table: false, pick: [], geom: null };
let redrawTimer = null;

const seriesColor = (i) =>
  getComputedStyle(document.documentElement)
    .getPropertyValue(SERIES_VARS[i % 3])
    .trim();
const scheduleRedraw = () => {
  if (!redrawTimer)
    redrawTimer = setTimeout(() => {
      redrawTimer = null;
      draw();
    }, 400);
};

async function loadHistory() {
  const kind = $("tlKind").value,
    hours = +$("tlRange").value;
  try {
    tl.data = await getJSON(`/api/history?kind=${kind}&hours=${hours}`);
    tl.range = hours;
    const have = (n) => tl.data.series[n];
    if (kind === "mood") tl.pick = MOOD_NAMES.filter(have);
    else {
      tl.pick = tl.pick.filter(have);
      if (!tl.pick.length)
        tl.pick = Object.keys(tl.data.series).slice(0, MAX_PICK);
    }
  } catch (e) {
    tl.data = null;
  }
  draw();
}

function gridAndAxes(W, y, x, t0, t1) {
  const m = MARGIN;
  let g = "";
  for (const v of [0, 25, 50, 75, 100]) {
    g += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  }
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const t = t0 + ((t1 - t0) * i) / ticks;
    g += `<text x="${x(t)}" y="${HEIGHT - m.b + 16}" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}">${esc(fmtClock(t, tl.range))}</text>`;
  }
  return g;
}

// Bottom strip of feelings derived from the three mood series (mood only).
function feelingsStrip(data, x, t0, t1) {
  const present = new Set();
  let strip = "";
  if (data.kind !== "mood" || !MOOD_NAMES.every((n) => data.series[n]))
    return { strip, present };
  let prev = null,
    startX = MARGIN.l;
  for (let i = 0; i <= FEELING_SAMPLES; i++) {
    const t = t0 + ((t1 - t0) * i) / FEELING_SAMPLES;
    const f = feeling({
      valence: valueAt(data.series.valence, t),
      energy: valueAt(data.series.energy, t),
      stress: valueAt(data.series.stress, t),
    });
    if (f !== prev || i === FEELING_SAMPLES) {
      if (prev) {
        strip += `<rect x="${startX}" y="${HEIGHT - MARGIN.b + 24}" width="${Math.max(0, x(t) - startX)}" height="12" fill="${FEELING_COLORS[prev]}" rx="2"><title>${prev}</title></rect>`;
        present.add(prev);
      }
      prev = f;
      startX = x(t);
    }
  }
  return { strip, present };
}

function seriesLines(names, data, x, y, t0, t1) {
  let lines = "",
    labels = "";
  names.forEach((n, i) => {
    const pts = (data.series[n] || []).filter((p) => p[0] <= t1);
    if (!pts.length) return;
    let path = "",
      lastY = null;
    pts.forEach((p, k) => {
      const px = Math.max(MARGIN.l, x(Math.max(p[0], t0))),
        py = y(p[1]);
      path += k === 0 ? `M${px},${py}` : `H${px}V${py}`; // step: a value holds until the next change
      lastY = py;
    });
    path += `H${x(t1)}`;
    lines += `<path class="ln" d="${path}" stroke="${seriesColor(i)}"/>`;
    labels += `<text x="${x(t1) + 6}" y="${lastY + 4}" style="fill:var(--ink)">${esc(n)}</text>`;
  });
  return lines + labels;
}

function drawLegend(names, data, present) {
  $("tlLegend").innerHTML =
    names
      .map(
        (n, i) =>
          `<span><span class="sw" style="background:${seriesColor(i)}"></span>${esc(n)}${live[data.kind]?.[n] != null ? ` <span class="muted">${Math.round(live[data.kind][n])}</span>` : ""}</span>`,
      )
      .join("") +
    (data.kind === "traits"
      ? ' <span class="muted">· pick up to 3:</span> ' +
        Object.keys(data.series)
          .map(
            (n) =>
              `<button data-trait="${esc(n)}" aria-pressed="${tl.pick.includes(n)}">${esc(n)}</button>`,
          )
          .join("")
      : "");
  document.querySelectorAll("#tlLegend button[data-trait]").forEach((b) => {
    b.onclick = () => {
      const n = b.dataset.trait;
      tl.pick = tl.pick.includes(n)
        ? tl.pick.filter((q) => q !== n)
        : [...tl.pick, n].slice(-MAX_PICK);
      draw();
    };
  });
  $("tlFeelings").innerHTML = present.size
    ? '<span class="muted">Feeling (bottom strip):</span> ' +
      [...present]
        .map(
          (f) =>
            `<span><span class="sw box" style="background:${FEELING_COLORS[f]}"></span>${f}</span>`,
        )
        .join("")
    : "";
}

function draw() {
  const svg = $("tlSvg"),
    data = tl.data;
  if (!data || !Object.keys(data.series).length) {
    svg.setAttribute("viewBox", "0 0 800 120");
    svg.innerHTML = `<text x="400" y="60" text-anchor="middle">No history recorded for this range yet. Points are saved whenever mood or traits change.</text>`;
    $("tlLegend").innerHTML = "";
    $("tlFeelings").innerHTML = "";
    $("tlTableWrap").style.display = "none";
    return;
  }
  const m = MARGIN;
  const W = Math.max(320, $("tlWrap").clientWidth || 800);
  const t1 = Date.now() / 1000,
    t0 = t1 - tl.range * 3600;
  const x = (t) => m.l + ((t - t0) / (t1 - t0)) * (W - m.l - m.r);
  const y = (v) => m.t + (1 - v / 100) * (HEIGHT - m.t - m.b);
  const names =
    data.kind === "mood" ? MOOD_NAMES.filter((n) => data.series[n]) : tl.pick;
  const { strip, present } = feelingsStrip(data, x, t0, t1);

  svg.setAttribute("viewBox", `0 0 ${W} ${HEIGHT}`);
  svg.innerHTML =
    gridAndAxes(W, y, x, t0, t1) +
    strip +
    seriesLines(names, data, x, y, t0, t1) +
    `<line id="tlCross" class="axis" y1="${m.t}" y2="${HEIGHT - m.b}" style="display:none"/>` +
    `<rect id="tlHit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${HEIGHT - m.t - m.b + 40}" fill="transparent"/>`;
  tl.geom = { W, t0, t1, names };

  drawLegend(names, data, present);
  const hit = $("tlHit");
  hit.onmousemove = hover;
  hit.onmouseleave = () => {
    $("tlTip").style.display = "none";
    $("tlCross").style.display = "none";
  };
  drawTable();
}

function hover(e) {
  const r = $("tlSvg").getBoundingClientRect(),
    g = tl.geom,
    data = tl.data;
  const px = ((e.clientX - r.left) / r.width) * g.W;
  const t =
    g.t0 + ((px - MARGIN.l) / (g.W - MARGIN.l - MARGIN.r)) * (g.t1 - g.t0);
  const vals = {};
  g.names.forEach((n) => {
    vals[n] = valueAt(data.series[n] || [], t);
  });
  const cross = $("tlCross");
  cross.setAttribute("x1", px);
  cross.setAttribute("x2", px);
  cross.style.display = "";
  let html =
    `<b>${esc(fmtClock(t, tl.range))}</b><br>` +
    g.names
      .map((n, i) =>
        vals[n] == null
          ? ""
          : `<span class="sw" style="background:${seriesColor(i)}"></span>${esc(n)} <b>${Math.round(vals[n])}</b>`,
      )
      .filter(Boolean)
      .join("<br>");
  if (data.kind === "mood" && vals.valence != null)
    html += `<br>feeling: <b>${feeling(vals)}</b>`;
  const tip = $("tlTip");
  tip.innerHTML = html;
  tip.style.display = "block";
  // Report only where the cursor is (a measured coordinate); the gap and the side
  // the tooltip sits on are CSS (.tip). If the default placement would leave the
  // chart area, flip it to the other side of the cursor.
  const wrap = $("tlWrap").getBoundingClientRect();
  tip.style.setProperty("--tip-x", `${e.clientX - wrap.left}px`);
  tip.style.setProperty("--tip-y", `${e.clientY - wrap.top}px`);
  tip.classList.remove("flip-x", "flip-y");
  const box = tip.getBoundingClientRect();
  tip.classList.toggle("flip-x", box.right > wrap.right);
  tip.classList.toggle("flip-y", box.top < wrap.top);
}

function drawTable() {
  const wrap = $("tlTableWrap"),
    data = tl.data;
  wrap.style.display = tl.table ? "block" : "none";
  if (!tl.table || !data) return;
  const names = tl.geom.names;
  const isMood = data.kind === "mood";
  const times = [
    ...new Set(names.flatMap((n) => (data.series[n] || []).map((p) => p[0]))),
  ]
    .sort((a, b) => b - a)
    .slice(0, 60);
  wrap.innerHTML =
    `<table class="t"><tr><th>time</th>${names.map((n) => `<th>${esc(n)}</th>`).join("")}${isMood ? "<th>feeling</th>" : ""}</tr>` +
    times
      .map((t) => {
        const v = {};
        names.forEach((n) => {
          v[n] = valueAt(data.series[n] || [], t);
        });
        return `<tr><td>${esc(new Date(t * 1000).toLocaleString())}</td>${names.map((n) => `<td>${v[n] == null ? "" : v[n].toFixed(1)}</td>`).join("")}${isMood ? `<td>${feeling(v)}</td>` : ""}</tr>`;
      })
      .join("") +
    "</table>";
}

export function initTimeline() {
  $("tlKind").onchange = () => {
    if ($("tlKind").value === "traits") tl.pick = [];
    loadHistory();
  };
  $("tlRange").onchange = loadHistory;
  $("tlTable").onclick = () => {
    tl.table = !tl.table;
    $("tlTable").setAttribute("aria-pressed", tl.table);
    draw();
  };

  on("tab", (name) => {
    if (name === "timeline") loadHistory();
  });
  on("theme", draw);
  addEventListener("resize", scheduleRedraw);
  // Live points: extend whichever series is on screen without a reload.
  on("stream", (ev) => {
    if (ev.type !== "personality.mood" && ev.type !== "personality.traits")
      return;
    const kind = ev.type === "personality.mood" ? "mood" : "traits";
    if (!tl.data || tl.data.kind !== kind) return;
    for (const [k, v] of Object.entries(ev.data.values))
      (tl.data.series[k] ||= []).push([ev.ts, v]);
    scheduleRedraw();
  });
}
