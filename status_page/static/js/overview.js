// Overview tab (services, endpoints, companion, placement), the problems banner,
// the memory list, and the chat history. Owns the poll that feeds everyone else.
import { $, esc, fmtTime, ago, getJSON } from "./util.js";
import { on, emit } from "./bus.js";
import { compareHtml, wasCleaned } from "./diff.js";

const POLL_MS = 3_000;

// Chat messages whose raw-response comparison the viewer has open. The chat is
// re-rendered on every poll, which would otherwise close it again.
const openRaw = new Set();

function row(state, label, detail) {
  return `<div class="row"><span class="dot ${state}"></span><span class="grow">${esc(label)}</span><span class="muted">${esc(detail)}</span></div>`;
}

const kvList = (pairs) =>
  pairs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("") ||
  '<dd class="muted">no data</dd>';

const plural = (n, unit) => `${n} ${unit}${n === 1 ? "" : "s"}`;
const pad = (n) => String(n).padStart(2, "0");
const stamp = (t) => {
  const d = new Date(t * 1_000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

// "in 8 hours, 20 minutes" once an hour or more away; "in 20 minutes, 30 seconds"
// inside the last hour (so it visibly counts down by the second).
const countdown = (remaining) => {
  const s = Math.max(0, Math.floor(remaining));
  const h = Math.floor(s / 3_600),
    m = Math.floor((s % 3_600) / 60);
  if (h >= 1) return `in ${plural(h, "hour")}, ${plural(m, "minute")}`;
  if (m >= 1) return `in ${plural(m, "minute")}, ${plural(s % 60, "second")}`;
  return s > 0 ? `in ${plural(s, "second")}` : "due now";
};

// Next scheduled backup, as reported by the timer; ticks on its own clock
// (corrected to the server's) between polls.
const BACKUP_SOON_S = 5 * 60; // bold the line once the next backup is this close
let nextBackup = null;
let clockSkew = 0;
function tickBackup() {
  const el = $("backupNext");
  if (!el) return;
  if (nextBackup == null) {
    el.textContent = "";
    return;
  }
  const remaining = nextBackup - (Date.now() / 1_000 + clockSkew);
  el.textContent = `Next backup scheduled: ${stamp(nextBackup)} (${countdown(remaining)})`;
  el.classList.toggle("soon", remaining < BACKUP_SOON_S);
}

// Picture of the current look; only reassigned when the version changes so the
// 3 s poll does not reload it.
let pictureVersion = null;
function showPlacementPicture(version) {
  const img = $("placementPic");
  if (version === pictureVersion) return;
  pictureVersion = version;
  if (version == null) {
    img.hidden = true;
    return;
  }
  img.alt = `Version ${version}`;
  img.onload = () => (img.hidden = false);
  img.onerror = () => (img.hidden = true);
  img.src = `/api/looks/${version}.png`;
}

function render(d) {
  const problems = [];

  $("units").innerHTML = d.units
    .map((u) => {
      const ok = u.active === "active";
      if (!ok) problems.push(`${u.unit} is ${u.active}`);
      return row(
        ok ? "ok" : "bad",
        u.unit,
        `${u.active}${u.restarts ? ` · ${u.restarts} restarts` : ""}`,
      );
    })
    .join("");

  const h = d.brain.health,
    s = d.brain.status;
  const brainErr = h.error || s.error;
  if (brainErr)
    problems.push(`brain unreachable at ${d.brain.url} (${brainErr})`);
  $("probes").innerHTML =
    row(brainErr ? "bad" : "ok", "brain api", d.brain.url) +
    d.probes
      .map((p) =>
        row(
          !p.enabled ? "warn" : p.ok ? "ok" : "bad",
          p.name,
          p.ok ? `${p.ms} ms` : p.detail,
        ),
      )
      .join("");
  d.probes
    .filter((p) => p.enabled && !p.ok)
    .forEach((p) => problems.push(`${p.name} not responding (${p.detail})`));

  const companion = [];
  if (!h.error)
    companion.push(
      ["name", h.name],
      ["voice", h.active_voice + (h.voice_degraded ? " (degraded)" : "")],
      ["presence", h.presence],
    );
  if (!s.error) {
    companion.push(["look", `v${s.look.current_version} — ${s.look.summary}`]);
    companion.push([
      "overlay",
      s.overlay_clients.length
        ? `connected (${s.overlay_clients.length})`
        : "not connected",
    ]);
    companion.push(["uptime", ago(d.now - s.started_at)]);
    if (s.build_running) companion.push(["blender", "build running"]);
    if (!s.overlay_clients.length)
      problems.push("overlay is not connected to the brain");
  }
  $("companion").innerHTML = kvList(companion);

  const b = d.backup;
  const backup = [];
  if (!b.configured)
    problems.push(
      "no [storage] backup_dir is configured: state is not being backed up",
    );
  else {
    backup.push(["target", b.path]);
    const age = b.last_ts ? d.now - b.last_ts : null;
    backup.push([
      "last backup",
      age == null
        ? "never"
        : `${ago(age)} ago (${new Date(b.last_ts * 1_000).toLocaleString()})`,
    ]);
    if (age == null) problems.push("no backup has been made yet");
    else if (age > b.stale_after_s)
      problems.push(`the last backup is ${ago(age)} old`);
  }
  backup.push(["timer", b.timer]);
  if (b.timer !== "active")
    problems.push(
      "the daily backup timer (deskmate-backup.timer) is not active",
    );
  $("backup").innerHTML = kvList(backup);
  clockSkew = d.now - Date.now() / 1_000;
  nextBackup = b.next || null;
  tickBackup();

  const placement = [];
  const pl = s.placement,
    w = s.window;
  if (pl)
    placement.push([
      "avatar",
      `${pl.x}, ${pl.y} on ${pl.screen}${pl.hidden ? " (hidden)" : ""}`,
    ]);
  if (w)
    placement.push([
      "active window",
      `${w.w}×${w.h} at ${w.x}, ${w.y} on ${w.screen}${w.fullscreen ? " (fullscreen)" : ""}`,
    ]);
  if (s.screens)
    placement.push([
      "screens",
      s.screens.map((x) => `${x.name} ${x.w}×${x.h}`).join(" · ") || "none",
    ]);
  $("placement").innerHTML = kvList(placement);
  showPlacementPicture(s.error ? pictureVersion : (s.look && s.look.current_version) ?? null);

  $("memories").innerHTML =
    d.db.memories.map((m) => `<li>${esc(m.note)}</li>`).join("") ||
    '<li class="muted">none</li>';
  const chat = $("chat");
  const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 40;
  chat.innerHTML =
    d.db.interactions
      .map((m) => {
        // Assistant turns saved since the raw reply was recorded carry it, so the
        // model's own words can be compared with what was said and stored.
        const raw =
          m.raw == null
            ? ""
            : `<details class="rawmsg" data-id="${m.id}"${openRaw.has(m.id) ? " open" : ""}><summary>raw response${wasCleaned(m.raw, m.text) ? " · cleaned" : ""}</summary>${compareHtml(m.raw, m.text)}</details>`;
        return `<div class="msg ${esc(m.role)}"><small>${esc(m.role)} · ${fmtTime(m.ts)}${m.provider ? " · " + esc(m.provider) : ""}${m.escalated ? " · escalated" : ""}</small>${esc(m.text)}${raw}</div>`;
      })
      .join("") || '<span class="muted">no messages</span>';
  if (atBottom) chat.scrollTop = chat.scrollHeight;
  if (d.db.error) problems.push("state.db: " + d.db.error);

  const banner = $("banner");
  banner.style.display = problems.length ? "block" : "none";
  banner.innerHTML = problems.map(esc).join("<br>");
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();
}

async function poll() {
  try {
    emit("overview", await getJSON("/api/overview"));
  } catch (e) {
    $("updated").textContent = "status service unreachable (" + e.message + ")";
  }
}

export function initOverview() {
  // `toggle` does not bubble, so listen in the capture phase on the container.
  $("chat").addEventListener(
    "toggle",
    (e) => {
      const id = Number(e.target.dataset && e.target.dataset.id);
      if (!id) return;
      if (e.target.open) openRaw.add(id);
      else openRaw.delete(id);
    },
    true,
  );
  on("overview", render);
  poll();
  setInterval(poll, POLL_MS);
  setInterval(tickBackup, 1_000);
}
