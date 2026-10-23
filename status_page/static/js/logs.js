// Logs tab: tail one source at a time (brain, overlay, TTS servers...) over SSE.
// Sources are named by this service's config; the page only ever picks a name.
import { $, esc } from "./util.js";
import { on } from "./bus.js";

const MAX_LINES = 1_500;
const REDRAW_MS = 250;
const RECONNECT_MS = 2_000;

const lines = [];
let current = null,
  source = null,
  tabsKey = "",
  names = [],
  paused = false,
  filter = "",
  redrawPending = false;

const lineClass = (l) =>
  /\b(ERROR|CRITICAL|Traceback|Exception)\b/.test(l)
    ? "err"
    : /\bWARNING\b/.test(l)
      ? "wrn"
      : "";

function draw() {
  const f = filter.toLowerCase();
  const pre = $("log");
  const stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.innerHTML = lines
    .filter((l) => !f || l.toLowerCase().includes(f))
    .map((l) => `<span class="${lineClass(l)}">${esc(l)}</span>`)
    .join("\n");
  if (stick) pre.scrollTop = pre.scrollHeight;
}

function scheduleDraw() {
  if (redrawPending) return;
  redrawPending = true;
  setTimeout(() => {
    redrawPending = false;
    if (!paused) draw();
  }, REDRAW_MS);
}

function open(name) {
  if (source) source.close();
  current = name;
  lines.length = 0;
  draw();
  buildTabs(null, true);
  source = new EventSource("/api/logs/" + encodeURIComponent(name));
  source.onmessage = (e) => {
    lines.push(JSON.parse(e.data));
    if (lines.length > MAX_LINES) lines.splice(0, lines.length - MAX_LINES);
    scheduleDraw();
  };
  // Re-open instead of relying on EventSource's auto-reconnect, which would
  // replay the backlog as duplicate lines.
  source.onerror = () => {
    source.close();
    setTimeout(() => {
      if (current === name) open(name);
    }, RECONNECT_MS);
  };
}

function buildTabs(sourceNames, force) {
  if (sourceNames) {
    const key = sourceNames.join("|");
    if (key === tabsKey && !force) return;
    tabsKey = key;
    names = sourceNames;
  }
  $("logtabs").innerHTML =
    names
      .map(
        (n) =>
          `<button data-n="${esc(n)}" aria-pressed="${n === current}">${esc(n)}</button>`,
      )
      .join("") +
    `<span class="spacer"></span><input id="flt" placeholder="filter…" value="${esc(filter)}" size="14"><button id="pause" aria-pressed="${paused}">Pause</button><button id="clr">Clear</button>`;
  $("logtabs")
    .querySelectorAll("button[data-n]")
    .forEach((b) => {
      b.onclick = () => open(b.dataset.n);
    });
  $("flt").oninput = (e) => {
    filter = e.target.value;
    draw();
  };
  $("pause").onclick = () => {
    paused = !paused;
    $("pause").setAttribute("aria-pressed", paused);
    if (!paused) draw();
  };
  $("clr").onclick = () => {
    lines.length = 0;
    draw();
  };
  if (!current && names.length) open(names[0]);
}

export function initLogs() {
  on("overview", (d) => buildTabs(d.log_sources));
  on("tab", (name) => {
    if (name === "logs") draw();
  });
}
