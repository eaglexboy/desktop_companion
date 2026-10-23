// Overview tab (services, endpoints, companion, placement), the problems banner,
// the memory list, and the chat history. Owns the poll that feeds everyone else.
import { $, esc, fmtTime, ago, getJSON } from "./util.js";
import { on, emit } from "./bus.js";

const POLL_MS = 3000;

function row(state, label, detail) {
  return `<div class="row"><span class="dot ${state}"></span><span class="grow">${esc(label)}</span><span class="muted">${esc(detail)}</span></div>`;
}

const kvList = (pairs) =>
  pairs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("") ||
  '<dd class="muted">no data</dd>';

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
        : `${ago(age)} ago (${new Date(b.last_ts * 1000).toLocaleString()})`,
    ]);
    if (age == null) problems.push("no backup has been made yet");
    else if (age > b.stale_after_s)
      problems.push(`the last backup is ${ago(age)} old`);
  }
  backup.push([
    "timer",
    b.timer +
      (b.next ? ` · next ${new Date(b.next * 1000).toLocaleString()}` : ""),
  ]);
  if (b.timer !== "active")
    problems.push(
      "the daily backup timer (deskmate-backup.timer) is not active",
    );
  $("backup").innerHTML = kvList(backup);

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

  $("memories").innerHTML =
    d.db.memories.map((m) => `<li>${esc(m.note)}</li>`).join("") ||
    '<li class="muted">none</li>';
  const chat = $("chat");
  const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 40;
  chat.innerHTML =
    d.db.interactions
      .map(
        (m) =>
          `<div class="msg ${esc(m.role)}"><small>${esc(m.role)} · ${fmtTime(m.ts)}${m.provider ? " · " + esc(m.provider) : ""}${m.escalated ? " · escalated" : ""}</small>${esc(m.text)}</div>`,
      )
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
  on("overview", render);
  poll();
  setInterval(poll, POLL_MS);
}
