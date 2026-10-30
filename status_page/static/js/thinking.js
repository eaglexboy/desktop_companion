// Thinking tab: one card per user turn, built from the brain's event stream.
// Events carry a `turn` id; everything one message caused lands on one card.
import { $, esc, fmtTime, sec, chips } from "./util.js";
import { on } from "./bus.js";
import { isShown } from "./tabs.js";
import { compareHtml, wasCleaned } from "./diff.js";

const MAX_CARDS = 80;
const turns = new Map();   // turn id (or "bg<seq>") -> { ts, events, start, el }
let showMinor = false;

// ---- how each event type reads in the feed ----
function stepHtml(ev, c) {
  const d = ev.data;
  switch (ev.type) {
    case "turn.prompt":
      return `<div class="step"><details><summary>Prompt it was given (personality, mood, memories, look, skills)</summary><pre>${esc(d.system_prompt)}</pre></details></div>`;
    case "router.keyword":
      return `<div class="step decision">Keyword match — ran the <b>${esc(d.tool)}</b> tool instead of asking a model.</div>`;
    case "router.classified":
      return `<div class="step decision"><b>Routing:</b> ${d.escalate ? `wants the cloud model (${esc(d.capability)})` : "local model is enough"} ${chips([d.reason && `“${d.reason}”`, sec(d.ms)])}</div>`;
    case "router.classify_failed":
      return `<div class="step decision"><b>Routing:</b> classifier failed, defaulting to local ${chips([sec(d.ms)])}<br><span class="muted">${esc(d.error)}</span></div>`;
    case "router.route":
      return `<div class="step decision"><b>Sent to:</b> ${esc(d.pool)} pool${d.fell_back_to_local ? " — wanted cloud but none is configured, fell back to local" : ""}</div>`;
    case "llm.call":
      return `<div class="step">LLM call ${chips([d.purpose, d.model, sec(d.ms), d.prompt_tokens != null ? `${d.prompt_tokens} → ${d.completion_tokens} tokens` : "", d.has_reasoning ? "reasoned" : ""])}</div>`;
    case "llm.error":
      return `<div class="step decision" style="border-color:var(--bad)">LLM call failed ${chips([d.purpose, d.model, d.error])}</div>`;
    case "llm.reasoning":
      return `<div class="step reasoning"><details open><summary>Reasoning (${esc(d.purpose || "model")})</summary><pre>${esc(d.text)}</pre></details></div>`;
    case "reply.raw": {
      // Compared with the text that was finally said/saved once the turn has one.
      const said = c && c.events.find((e) => e.type === "turn.reply");
      const saved = said ? said.data.text : null;
      const flag = saved != null && wasCleaned(d.text, saved) ? chips(["cleaned"]) : "";
      return `<div class="step"><details open><summary>Raw response ${flag}</summary>${compareHtml(d.text, saved)}</details></div>`;
    }
    case "reply.parsed":
      return d.structured
        ? `<div class="step decision"><b>Reply format:</b> structured ${chips([`${d.actions.length} action${d.actions.length === 1 ? "" : "s"} proposed`])}</div>`
        : `<div class="step decision" style="border-color:var(--warn)"><b>Reply format:</b> not structured — the whole text was treated as dialogue, no actions run</div>`;
    case "skill.call": {
      const args = Object.entries(d.args || {}).map(([k, v]) => `${k}: ${v}`).join(", ");
      return `<div class="step ${d.ok ? "reply" : "decision"}" ${d.ok ? "" : 'style="border-color:var(--bad)"'}><b>Did:</b> ${esc(d.skill)} ${chips([args, sec(d.ms), d.ok ? (d.result || "ok") : "failed: " + d.error])}</div>`;
    }
    case "skill.rejected":
      return `<div class="step decision" style="border-color:var(--bad)"><b>Refused:</b> ${esc(d.skill)} <span class="muted">${esc(d.reason)}</span></div>`;
    case "turn.reply":
      return `<div class="step reply"><b>Said:</b> ${esc(d.text)} ${chips([d.provider, d.escalated ? "escalated" : ""])}</div>`;
    case "speech.chunk":
      return `<div class="step">Voice ${chips([`sentence ${d.seq}/${d.total}`, d.backend, d.seed != null ? "seed " + d.seed : "", sec(d.synth_s * 1000)])}</div>`;
    case "reflection.result": {
      const fmt = (o) => Object.entries(o || {}).map(([k, v]) => `${k} ${v > 0 ? "+" : ""}${(+v).toFixed(1)}`);
      return `<div class="step reasoning"><b>Reflection:</b> reviewed ${d.interactions_reviewed} messages ${chips([...fmt(d.mood_deltas), ...fmt(d.trait_deltas)])}${(d.memory_notes || []).map((n) => `<br>• ${esc(n)}`).join("")}</div>`;
    }
    case "autonomous.check": {
      const verdict = {
        build_running: "a build is already running",
        user_interacting: "you were talking to it",
        staged_pending: "a new look is already built and waiting for you to come back",
        cooldown: `still cooling down (${d.elapsed_h}h of ${d.threshold_h}h)`,
        roll_failed: `rolled ${d.roll}, needed under ${d.needed_below} — decided not to this time`,
        triggered: `rolled ${d.roll} — decided to try a new look`,
      }[d.outcome] || esc(d.outcome);
      return `<div class="step decision"><b>Look change check:</b> ${verdict}</div>`;
    }
    case "appearance.designing":
    case "appearance.building":
    case "appearance.done":
    case "appearance.staged":
    case "appearance.failed":
      return `<div class="step ${ev.type === "appearance.failed" ? "decision" : "reply"}"${ev.type === "appearance.failed" ? ' style="border-color:var(--bad)"' : ""}><b>Look change (${esc(d.source)}):</b> ${esc(d.label)} ${chips([d.version != null && `v${d.version}`, d.error])}</div>`;
    case "appearance.unmet":
      return `<div class="step decision"><b>Look change:</b> couldn't build “${esc(d.request)}” — missing ${esc(d.missing)}</div>`;
    case "presence.transition":
      return `<div class="step decision"><b>You came back</b> (${esc(d.from)} → ${esc(d.to)}): ${d.greeted ? "greeted you" : "no greeting (cooldown or disabled)"}${d.revealed ? ", revealed the staged look" : ""}</div>`;
    default:
      return "";
  }
}

// Background decisions worth seeing without "Background events" switched on.
// The routine hourly "not yet" outcomes stay behind that toggle.
const QUIET_CHECKS = new Set(["build_running", "user_interacting", "staged_pending", "cooldown"]);
const isNotable = (ev) =>
  ev.type.startsWith("appearance.") ||
  ev.type === "presence.transition" ||
  (ev.type === "autonomous.check" && !QUIET_CHECKS.has(ev.data.outcome));

function cardHtml(c) {
  const head = c.start ? esc(c.start.data.text) : "(background)";
  return `<h3><span>${c.start ? "You:" : "Background:"}</span><span style="font-weight:400">${head}</span><span class="t">${fmtTime(c.ts)}</span></h3>` + c.events.map((e) => stepHtml(e, c)).join("");
}

// ---- feed management ----
// The "Nothing yet" placeholder goes only when a card is really about to be added.
function clearPlaceholder() {
  const feed = $("feed");
  if (feed.querySelector(".empty")) feed.innerHTML = "";
}
function trim() {
  const feed = $("feed");
  while (feed.children.length > MAX_CARDS) {
    // Background cards go first so they can't push real conversations out.
    const first = feed.querySelector(".minor") || feed.firstChild;
    for (const [k, v] of turns) if (v.el === first) turns.delete(k);
    feed.removeChild(first);
  }
}
function addCard(id, ev, minor = false) {
  const c = { ts: ev.ts, events: [ev], el: document.createElement("div") };
  c.el.className = minor ? "turn minor" : "turn";
  c.el.innerHTML = cardHtml(c);
  turns.set(id, c);
  clearPlaceholder();
  $("feed").appendChild(c.el);
  trim();
}
function addMinor(ev, text) {
  const el = document.createElement("div");
  el.className = "turn minor";
  el.textContent = fmtTime(ev.ts) + " · " + text;
  clearPlaceholder();
  $("feed").appendChild(el);
  trim();
}
function noteReasoning(d) {
  if (d.purpose === "classify") return;
  $("reasoningNote").textContent = d.reasoning_enabled
    ? "Reasoning is ON for the companion's replies (reasoning = true under [llm.local]). The model's thinking appears below, never spoken."
    : "Reasoning is OFF for the companion's replies. To watch it think, set  reasoning = true  under [llm.local] in config.toml (replies get slower).";
}

function onStream(ev) {
  const t = ev.type;
  if (t === "personality.mood" || t === "personality.traits") {
    if (ev.data.cause !== "decay" && ev.data.cause !== "startup") {
      const parts = Object.entries(ev.data.deltas || {}).map(([k, v]) => `${k} ${v > 0 ? "+" : ""}${(+v).toFixed(1)}`).join(", ");
      addMinor(ev, `${t === "personality.mood" ? "mood" : "traits"} changed (${ev.data.cause}): ${parts}`);
    }
    return;
  }
  if (t === "llm.call") noteReasoning(ev.data);

  const feed = $("feed");
  const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;

  if (t === "presence.placement") {
    const d = ev.data;
    addMinor(ev, `Placed avatar at ${d.x}, ${d.y} on ${d.screen}${d.hidden ? " (hidden)" : ""}${d.window ? " — window " + d.window.w + "×" + d.window.h : ""}`);
    return;
  }
  if (!ev.turn) {
    if (t === "reflection.result" || isNotable(ev)) addCard("bg" + ev.seq, ev);
    else if (stepHtml(ev, null)) addCard("bg" + ev.seq, ev, true);
    return;
  }
  let c = turns.get(ev.turn);
  if (!c) {
    c = { ts: ev.ts, events: [], el: document.createElement("div") };
    c.el.className = "turn";
    turns.set(ev.turn, c);
    clearPlaceholder();
    feed.appendChild(c.el);
    trim();
  }
  if (t === "turn.start") c.start = ev;
  c.events.push(ev);
  c.el.innerHTML = cardHtml(c);
  if (stick && isShown("thinking")) feed.scrollTop = feed.scrollHeight;
}

export function initThinking() {
  on("stream", onStream);
  on("tab", (name) => { if (name === "thinking") $("feed").scrollTop = $("feed").scrollHeight; });
  // Background cards are always recorded and only hidden, so the toggle
  // works on what's already in the feed (and brings back what was missed).
  const feed = $("feed");
  feed.classList.add("hide-minor");
  $("showMinor").onclick = () => {
    showMinor = !showMinor;
    $("showMinor").setAttribute("aria-pressed", showMinor);
    feed.classList.toggle("hide-minor", !showMinor);
  };
  $("clearFeed").onclick = () => { $("feed").innerHTML = '<div class="empty">Cleared.</div>'; turns.clear(); };
}
