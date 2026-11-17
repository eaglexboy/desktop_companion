// Resource use: the history page (a chart per metric, one line per process, restarts marked,
// hover readout, table view) and the current values on the Overview. Data is sampled by the
// status service (status_page/resources.py) and read from /api/resources.
import { $, esc, fmtClock, getJSON } from "./util.js";
import { on } from "./bus.js";
import { valueAt } from "./series.js";

const SERIES_VARS = ["--s1", "--s2", "--s3", "--s4", "--s5"];
const MARGIN = { l: 68, r: 12, t: 10, b: 28 };
const HEIGHT = 300;
const TICKS = 4;
const TABLE_ROWS = 60;
const REFRESH_MS = 30_000;
const TREND_MIN_MB = 5; // a 6 h rise smaller than this is not worth flagging

const METRICS = [
  ["anon", "Private memory"],
  ["rss", "Resident memory"],
  ["pss", "Proportional memory"],
  ["cpu", "CPU"],
  ["gpu", "GPU memory"],
  ["gpu_util", "GPU utilization"],
  ["gpu_temp", "GPU temperature"],
  ["gpu_power", "GPU power"],
  ["threads", "Threads"],
  ["fds", "Open files"],
];

const MAX_PICK = SERIES_VARS.length; // the validated categorical colors

/** @type {Record<string, any>} one entry per metric: its data, picked series and drawn geometry */
const charts = {};
const rs = { table: false, hours: 6 };

const seriesColor = (i) =>
  getComputedStyle(document.documentElement)
    .getPropertyValue(SERIES_VARS[i % SERIES_VARS.length])
    .trim();

const fmtIn = (c, v) =>
  v == null
    ? ""
    : `${Math.abs(v) >= 100 ? Math.round(v) : +v.toFixed(1)}${c.data.unit ? " " + c.data.unit : ""}`;

// The top of the axis: the data maximum rounded up to 1, 2 or 5 times a power of ten.
function niceMax(max) {
  if (!(max > 0)) return 1;
  const pow = 10 ** Math.floor(Math.log10(max));
  const f = max / pow;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * pow;
}

const part = (metric, name) => $(`rs-${metric}-${name}`);

function buildCharts() {
  $("rsCharts").innerHTML = METRICS.map(
    ([k, label]) => `<section class="wide" id="rs-${k}-box" hidden>
      <h2>${esc(label)}</h2>
      <div class="legend" id="rs-${k}-legend"></div>
      <div class="chart-wrap" id="rs-${k}-wrap"><svg class="chart" id="rs-${k}-svg" role="img" aria-label="${esc(label)} over time"></svg>
        <div class="tip" id="rs-${k}-tip"></div>
      </div>
      <div class="rs-table" id="rs-${k}-table" hidden></div>
    </section>`,
  ).join("");
}

async function loadOne(metric) {
  try {
    const data = await getJSON(
      `/api/resources?metric=${metric}&hours=${rs.hours}`,
    );
    const c = (charts[metric] ??= { pick: [], geom: null });
    c.data = data;
    choosePick(c, Object.keys(data.series));
  } catch (e) {
    delete charts[metric];
  }
  draw(metric);
}

async function load() {
  rs.hours = +$("rsRange").value;
  await Promise.all(METRICS.map(([k]) => loadOne(k)));
  const restarts = Object.values(charts)[0]?.data.restarts.length ?? 0;
  $("rsHint").textContent = restarts
    ? `Dashed lines mark a restart of a service (${restarts} in this range).`
    : "A memory line that only climbs, hour after hour and across idle time, is a leak; one that rises and falls back is normal use.";
}

// Which series to draw: at most as many as there are validated colors. Keeps the ones still present;
// otherwise the series that reach the highest values, since those are what a leak shows in.
function choosePick(c, all) {
  c.pick = c.pick.filter((n) => all.includes(n));
  if (c.pick.length) return;
  const peak = (n) => Math.max(...c.data.series[n].map((p) => p[1]));
  c.pick = [...all].sort((a, b) => peak(b) - peak(a)).slice(0, MAX_PICK);
}

function drawAll() {
  METRICS.forEach(([k]) => draw(k));
}

function draw(metric) {
  const c = charts[metric],
    box = $(`rs-${metric}-box`);
  const all = c ? Object.keys(c.data.series) : [];
  box.hidden = !all.length; // a metric nothing reports (no GPU here, say) gets no empty chart
  if (!all.length) {
    if (c) c.geom = null;
    $("rsEmpty").hidden = METRICS.some(([k]) => charts[k]?.geom);
    return;
  }
  $("rsEmpty").hidden = true;
  const data = c.data;
  let names = all.filter((n) => c.pick.includes(n));
  if (!names.length) {
    c.pick = all.slice(0, 1); // nothing selected: show one rather than a blank chart
    names = c.pick;
  }
  const svg = part(metric, "svg");
  const m = MARGIN;
  const W = Math.max(320, part(metric, "wrap").clientWidth || 800);
  const t1 = data.now,
    t0 = data.since;
  const top = niceMax(
    Math.max(...names.flatMap((n) => data.series[n].map((p) => p[1]))),
  );
  const x = (t) => m.l + ((t - t0) / (t1 - t0)) * (W - m.l - m.r);
  const y = (v) => m.t + (1 - v / top) * (HEIGHT - m.t - m.b);

  let g = "";
  for (let i = 0; i <= TICKS; i++) {
    const v = (top * i) / TICKS;
    g += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${esc(fmtIn(c, v))}</text>`;
  }
  for (let i = 0; i <= 5; i++) {
    const t = t0 + ((t1 - t0) * i) / 5;
    g += `<text x="${x(t)}" y="${HEIGHT - m.b + 16}" text-anchor="${i === 0 ? "start" : i === 5 ? "end" : "middle"}">${esc(fmtClock(t, rs.hours))}</text>`;
  }
  // A restarted service starts from scratch, so mark it: a drop there is not a release.
  const marks = data.restarts
    .filter((r) => r[0] >= t0)
    .map(
      (r) =>
        `<line class="restart" x1="${x(r[0])}" x2="${x(r[0])}" y1="${m.t}" y2="${HEIGHT - m.b}"><title>${esc(r[1])} restarted (pid ${r[2]})</title></line>`,
    )
    .join("");
  const lines = names
    .map((n, i) => {
      const d = data.series[n]
        .map((p, k) => `${k ? "L" : "M"}${x(p[0])},${y(p[1])}`)
        .join("");
      return `<path class="ln" d="${d}" stroke="${seriesColor(i)}"/>`;
    })
    .join("");

  svg.setAttribute("viewBox", `0 0 ${W} ${HEIGHT}`);
  svg.innerHTML =
    g +
    marks +
    lines +
    `<line id="rs-${metric}-cross" class="axis" y1="${m.t}" y2="${HEIGHT - m.b}" style="display:none"/>` +
    `<rect id="rs-${metric}-hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${HEIGHT - m.t - m.b}" fill="transparent"/>`;
  c.geom = { W, t0, t1, names };

  part(metric, "legend").innerHTML =
    all
      .map((n) => {
        const i = names.indexOf(n);
        const last = data.series[n][data.series[n].length - 1];
        const swatch =
          i < 0
            ? ""
            : `<span class="sw" style="background:${seriesColor(i)}"></span>`;
        return `<button data-series="${esc(n)}" aria-pressed="${i >= 0}">${swatch}${esc(n)} <span class="muted">${esc(fmtIn(c, last[1]))}</span></button>`;
      })
      .join("") +
    (all.length > MAX_PICK
      ? ` <span class="muted">· show up to ${MAX_PICK} at a time</span>`
      : "");
  part(metric, "legend")
    .querySelectorAll("button[data-series]")
    .forEach((b) => {
      b.onclick = () => {
        const n = b.dataset.series;
        c.pick = c.pick.includes(n)
          ? c.pick.filter((q) => q !== n)
          : [...c.pick, n].slice(-MAX_PICK);
        draw(metric);
      };
    });
  const hit = part(metric, "hit");
  hit.onmousemove = (e) => hover(metric, e);
  hit.onmouseleave = () => {
    part(metric, "tip").style.display = "none";
    part(metric, "cross").style.display = "none";
  };
  drawTable(metric);
}

function hover(metric, e) {
  const c = charts[metric],
    g = c.geom;
  const r = part(metric, "svg").getBoundingClientRect();
  const px = ((e.clientX - r.left) / r.width) * g.W;
  const t =
    g.t0 + ((px - MARGIN.l) / (g.W - MARGIN.l - MARGIN.r)) * (g.t1 - g.t0);
  const cross = part(metric, "cross");
  cross.setAttribute("x1", px);
  cross.setAttribute("x2", px);
  cross.style.display = "";
  const tip = part(metric, "tip");
  tip.innerHTML =
    `<b>${esc(new Date(t * 1_000).toLocaleString())}</b><br>` +
    g.names
      .map((n, i) => {
        const v = valueAt(c.data.series[n], t);
        return v == null
          ? ""
          : `<span class="sw" style="background:${seriesColor(i)}"></span>${esc(n)} <b>${esc(fmtIn(c, v))}</b>`;
      })
      .filter(Boolean)
      .join("<br>");
  tip.style.display = "block";
  const wrap = part(metric, "wrap").getBoundingClientRect();
  tip.style.setProperty("--tip-x", `${e.clientX - wrap.left}px`);
  tip.style.setProperty("--tip-y", `${e.clientY - wrap.top}px`);
  tip.classList.remove("flip-x", "flip-y");
  const box = tip.getBoundingClientRect();
  tip.classList.toggle("flip-x", box.right > wrap.right);
  tip.classList.toggle("flip-y", box.top < wrap.top);
}

function drawTable(metric) {
  const c = charts[metric],
    wrap = part(metric, "table");
  wrap.hidden = !rs.table || !c?.geom;
  if (wrap.hidden) return;
  const names = c.geom.names,
    data = c.data;
  const times = [
    ...new Set(names.flatMap((n) => data.series[n].map((p) => p[0]))),
  ]
    .sort((a, b) => b - a)
    .slice(0, TABLE_ROWS);
  wrap.innerHTML =
    `<table class="t"><tr><th>time</th>${names.map((n) => `<th>${esc(n)}</th>`).join("")}</tr>` +
    times
      .map(
        (t) =>
          `<tr><td>${esc(new Date(t * 1_000).toLocaleString())}</td>${names
            .map((n) => {
              const p = data.series[n].find((q) => q[0] === t);
              return `<td>${p ? esc(fmtIn(c, p[1])) : ""}</td>`;
            })
            .join("")}</tr>`,
      )
      .join("") +
    "</table>";
}

// --- the Overview's current values: a board with a tile per process ---------------

const STALE_S = 120; // a sample older than this is shown as stale
const SPARK_W = 100; // sparkline viewBox
const SPARK_H = 24;
const SPARK_HOURS = 6;
const SPARK_REFRESH_MS = 60_000;

const board = { last: null, sparks: { anon: null, gpu: null } };

const mb = (v) =>
  v == null
    ? "—"
    : v >= 1_024
      ? `${(v / 1_024).toFixed(1)} GB`
      : `${Math.round(v)} MB`;
const isCard = (proc) => proc === "gpu card" || proc.endsWith("/gpu card");
const hostOf = (proc) => proc.split("/")[0];
const nameOf = (proc) =>
  proc.includes("/") ? proc.slice(proc.indexOf("/") + 1) : proc;

// A line of a series' last few hours, scaled to its own range so a climb is visible at a glance.
function spark(points) {
  if (!points || points.length < 2)
    return '<svg class="spark" aria-hidden="true"></svg>';
  const t0 = points[0][0],
    t1 = points[points.length - 1][0];
  const lo = Math.min(...points.map((p) => p[1])),
    hi = Math.max(...points.map((p) => p[1]));
  const span = hi - lo || 1;
  const line = points
    .map(
      (p) =>
        `${(((p[0] - t0) / (t1 - t0 || 1)) * SPARK_W).toFixed(1)},${(SPARK_H - ((p[1] - lo) / span) * SPARK_H).toFixed(1)}`,
    )
    .join(" ");
  return `<svg class="spark" viewBox="0 0 ${SPARK_W} ${SPARK_H}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${line}"/></svg>`;
}

const meter = (label, pct, text) =>
  `<div class="meter"><span class="muted">${esc(label)}</span><span class="bar"><i style="width:${Math.max(0, Math.min(100, pct))}%"></i></span><span>${esc(text)}</span></div>`;

function processTile(r, now) {
  const stale = now - r.ts > STALE_S;
  const c = r.anon_change_6h;
  // Growth worth a second look: at least 5 MB and 5% of what it holds now.
  const rising =
    c != null && c >= Math.max(TREND_MIN_MB, 0.05 * (r.anon_mb || 0));
  const trend =
    c == null
      ? '<span class="trend muted">no trend yet</span>'
      : `<span class="trend${rising ? " up" : ""}">${c >= 0 ? "+" : "−"}${esc(mb(Math.abs(c)))} / ${Math.max(1, Math.round(r.window_s / 3_600))} h</span>`;
  const rows = [
    meter(
      "CPU",
      r.cpu_pct ?? 0,
      r.cpu_pct == null ? "—" : `${Math.round(r.cpu_pct)}%`,
    ),
  ];
  if (r.gpu_mb)
    rows.push(
      `<div class="fact"><span class="muted">GPU</span><span>${esc(mb(r.gpu_mb))}</span></div>`,
    );
  rows.push(
    `<div class="fact"><span class="muted">resident</span><span>${esc(mb(r.rss_mb))}</span></div>`,
  );
  rows.push(
    `<div class="fact"><span class="muted">threads</span><span>${r.threads ?? "—"}${r.fds != null ? ` · ${r.fds} files` : ""}</span></div>`,
  );
  return `<article class="res-tile${rising ? " rising" : ""}">
    <div class="res-head" title="pid ${r.pid ?? "—"}"><span class="dot ${stale ? "warn" : "ok"}"></span><b>${esc(nameOf(r.proc))}</b></div>
    <div class="big">${esc(mb(r.anon_mb))}<small>private memory</small></div>
    ${spark(board.sparks.anon && board.sparks.anon.series[r.proc])}
    ${trend}
    ${rows.join("")}
  </article>`;
}

function gpuTile(r, now) {
  const stale = now - r.ts > STALE_S;
  const rows = [
    meter(
      "busy",
      r.gpu_util_pct ?? 0,
      r.gpu_util_pct == null ? "—" : `${Math.round(r.gpu_util_pct)}%`,
    ),
  ];
  if (r.gpu_temp_c != null)
    rows.push(
      `<div class="fact"><span class="muted">temperature</span><span>${Math.round(r.gpu_temp_c)} °C</span></div>`,
    );
  if (r.gpu_power_w != null)
    rows.push(
      `<div class="fact"><span class="muted">power</span><span>${Math.round(r.gpu_power_w)} W</span></div>`,
    );
  return `<article class="res-tile gpu">
    <div class="res-head"><span class="dot ${stale ? "warn" : "ok"}"></span><b>GPU</b></div>
    <div class="big">${esc(mb(r.gpu_mb))}<small>memory in use</small></div>
    ${spark(board.sparks.gpu && board.sparks.gpu.series[r.proc])}
    ${rows.join("")}
  </article>`;
}

function renderNow(d) {
  if (d) board.last = d;
  d = board.last;
  if (!d) return;
  const list = d.resources || [];
  const hosts = new Map();
  for (const name of Object.keys(d.resource_sources || {})) hosts.set(name, []);
  for (const r of list) {
    const h = hostOf(r.proc);
    if (!hosts.has(h)) hosts.set(h, []);
    hosts.get(h).push(r);
  }
  const order = [...hosts.keys()].sort();
  const html = order
    .map((h) => {
      const rows = hosts.get(h);
      const src = (d.resource_sources || {})[h];
      const down =
        src && src.ok === false
          ? `<span class="res-down"><span class="dot bad"></span>sidecar unreachable (${esc(src.error)})</span>`
          : "";
      const tiles = rows
        .filter((r) => !isCard(r.proc))
        .map((r) => processTile(r, d.now))
        .concat(
          rows.filter((r) => isCard(r.proc)).map((r) => gpuTile(r, d.now)),
        );
      return `<div class="res-host"><h3>${esc(h)}${down}</h3><div class="res-grid">${tiles.join("") || '<span class="muted">no samples yet</span>'}</div></div>`;
    })
    .join("");
  $("resourceNow").innerHTML =
    html ||
    '<span class="muted">no samples yet (recorded every 30 seconds)</span>';
}

async function loadSparks() {
  try {
    const [anon, gpu] = await Promise.all([
      getJSON(`/api/resources?metric=anon&hours=${SPARK_HOURS}`),
      getJSON(`/api/resources?metric=gpu&hours=${SPARK_HOURS}`),
    ]);
    board.sparks = { anon, gpu };
    renderNow();
  } catch (e) {
    /* the tiles still show current values */
  }
}

export function initResources() {
  buildCharts();
  $("rsRange").onchange = load;
  $("rsTable").onclick = () => {
    rs.table = !rs.table;
    $("rsTable").setAttribute("aria-pressed", rs.table);
    METRICS.forEach(([k]) => drawTable(k));
  };
  on("overview", renderNow);
  loadSparks();
  setInterval(loadSparks, SPARK_REFRESH_MS);
  on("tab", (name) => {
    if (name === "resources") load();
  });
  on("theme", drawAll);
  addEventListener("resize", drawAll);
  setInterval(() => {
    if ($("tab-resources").classList.contains("on")) load();
  }, REFRESH_MS);
}
