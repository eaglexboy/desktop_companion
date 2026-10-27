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

const TIER_HELP = {
  imported: "fetched from a source; scan found nothing",
  flagged:
    "imported, with scripts, dangerous tools or scan findings: needs a human look before it may ever run",
  local: "placed by hand",
  bundled: "ships with the companion",
};

function renderInstalled(sk) {
  if (!sk || sk.error) {
    $("installedTable").innerHTML = '<div class="empty">Unavailable.</div>';
    return;
  }
  const inst = sk.installed || [];
  const rej = sk.rejected || [];
  const bud = sk.budget;
  $("installedSummary").textContent = bud
    ? `${bud.enabled}/${bud.installed} enabled · ${bud.pinned}/${bud.max_pinned} pinned · stubs ${bud.tokens_used}/${bud.budget_tokens} tokens (context ${bud.context_tokens}) · ${rej.length} refused`
    : `${inst.length} installed · ${rej.length} refused`;
  if (!inst.length && !rej.length) {
    $("installedTable").innerHTML =
      '<div class="empty">None installed. Try: python -m brain.skills.importer &lt;https-git-url | directory&gt;</div>';
    return;
  }
  const tier = (k) =>
    `<span class="dot ${k.tier === "flagged" ? "warn" : "ok"}" style="display:inline-block"></span> <span title="${esc(TIER_HELP[k.tier] || "")}">${esc(k.tier)}</span>`;
  const notes = (k) => {
    const p = k.provenance || {};
    const bits = [];
    if (p.scripts_in_source || k.has_scripts)
      bits.push(
        p.scripts_installed || k.has_scripts
          ? "scripts installed"
          : "scripts not installed",
      );
    (p.dangerous_tools || []).forEach((t) => bits.push("wants " + t));
    (p.findings || []).forEach((f) => bits.push("scan: " + f));
    (k.warnings || []).forEach((w) => bits.push(w));
    return bits.length
      ? bits.map((b) => `<span class="chip">${esc(b)}</span>`).join("")
      : '<span class="muted">none</span>';
  };
  const origin = (k) => {
    const p = k.provenance;
    if (!p) return '<span class="muted">local</span>';
    const cat = p.category
      ? `<br><span class="muted">${esc(p.category)}</span>`
      : "";
    return `${esc(p.source)} <span class="muted">${esc((p.commit || "").slice(0, 8))}</span>${cat}`;
  };
  const on = (k) =>
    `<span class="dot ${k.enabled ? "ok" : "warn"}" style="display:inline-block"></span> ${k.enabled ? "enabled" : "disabled"}` +
    (k.pinned
      ? ` · pinned <span class="muted">(${k.stub_tokens} tok)</span>`
      : "") +
    `<br><span class="muted" title="absent: not offered · discoverable: found by search · stub: always shown as a one-liner">${esc(k.exposure)}</span>`;
  $("installedTable").innerHTML =
    `<table class="t"><tr><th>skill</th><th>what it is for</th><th>trust</th><th>switch</th><th>from</th><th>license</th><th>notes</th><th>runs</th></tr>` +
    inst
      .map(
        (k) => `<tr>
      <td><b>${esc(k.display)}</b>${k.alias ? ` <span class="muted">(${esc(k.alias)})</span>` : ""}<br><span class="muted">${esc(k.id)}</span></td>
      <td>${esc(k.description)}</td><td>${tier(k)}</td><td>${on(k)}</td><td>${origin(k)}</td>
      <td>${esc(k.license || "unspecified")}</td><td>${notes(k)}</td>
      <td title="Waits for the agent runtime and sandbox (V2). Enabled skills are not put in the prompt yet."><span class="dot warn" style="display:inline-block"></span> not yet</td></tr>`,
      )
      .join("") +
    rej
      .map(
        (r) => `<tr>
      <td colspan="2"><b>${esc(r.path.split("/").slice(-2).join("/"))}</b></td>
      <td colspan="5" style="color:var(--bad)">${esc(r.reason)}</td>
      <td><span class="dot bad" style="display:inline-block"></span> rejected</td></tr>`,
      )
      .join("") +
    "</table>";
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
  on("overview", (d) => {
    renderRegistry(d.brain.skills);
    renderInstalled(d.brain.skills);
  });
  on("stream", (ev) => {
    if (ev.type === "skill.call" || ev.type === "skill.rejected") noteEvent(ev);
  });
}
