// Skills tab: the registry (from the brain's GET /api/skills, via the overview
// poll), a live list of skill runs/refusals, and the exact block the model is told.
import { $, esc, fmtTime, sec } from "./util.js";
import { on } from "./bus.js";

const ACTIVITY_MAX = 30;
const activity = [];

function renderRegistry(sk) {
  if (!sk || sk.error) {
    $("skillsTable").innerHTML =
      `<div class="empty">Skill registry unavailable (${esc(sk ? sk.error : "no data")}).</div>`;
    return;
  }
  $("skillsSummary").textContent = sk.enabled
    ? `skills on · up to ${sk.max_actions_per_reply} actions per reply`
    : "skills are OFF ([skills] enabled = false): replies are plain dialogue";
  const args = (s) =>
    s.params.length
      ? s.params
          .map(
            (p) =>
              `<span class="chip" title="${esc(p.description)}">${esc(p.name)}${p.enum ? ": " + esc(p.enum.join(" | ")) : ""}${p.required ? "" : " (optional)"}</span>`,
          )
          .join("")
      : '<span class="muted">none</span>';
  const state = (s) =>
    s.enabled ? (sk.enabled ? "enabled" : "off (skills disabled)") : "disabled";
  $("skillsTable").innerHTML =
    `<table class="t"><tr><th>skill</th><th>what it does</th><th>arguments</th><th>state</th><th>calls</th><th>failed</th><th>refused</th><th>last used</th></tr>` +
    sk.skills
      .map(
        (s) => `<tr>
      <td><b>${esc(s.name)}</b></td>
      <td>${esc(s.description)}</td>
      <td>${args(s)}</td>
      <td><span class="dot ${s.enabled && sk.enabled ? "ok" : "warn"}" style="display:inline-block"></span> ${state(s)}</td>
      <td>${s.calls}</td><td>${s.failures ? `<span style="color:var(--bad)">${s.failures}</span>` : 0}</td><td>${s.refused}</td>
      <td class="muted">${s.last_used ? fmtTime(s.last_used) : "never"}${s.last_error ? `<br><span style="color:var(--bad)">${esc(s.last_error)}</span>` : ""}</td></tr>`,
      )
      .join("") +
    "</table>";
  $("skillPrompt").textContent =
    sk.prompt_block || "(nothing: skills are off or none are enabled)";
}

function noteEvent(ev) {
  const d = ev.data;
  const args = Object.entries(d.args || {})
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");
  activity.unshift(
    ev.type === "skill.call"
      ? `${fmtTime(ev.ts)}  ${d.ok ? "ran" : "FAILED"}  ${d.skill}(${args})  ${d.ok ? d.result || "" : d.error}  [${sec(d.ms)}]`
      : `${fmtTime(ev.ts)}  REFUSED  ${d.skill}  ${d.reason}`,
  );
  activity.length = Math.min(activity.length, ACTIVITY_MAX);
  $("skillActivity").innerHTML =
    `<pre class="mono">${activity.map(esc).join("\n")}</pre>`;
}

export function initSkills() {
  on("overview", (d) => renderRegistry(d.brain.skills));
  on("stream", (ev) => {
    if (ev.type === "skill.call" || ev.type === "skill.rejected") noteEvent(ev);
  });
}
