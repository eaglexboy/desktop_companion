// What the model returned versus what was said and saved. Shared by the
// Thinking cards and the Chat tab; pure functions of strings, no DOM, no state.
import { esc } from "./util.js";

// A word-level LCS table is O(n*m); replies are a few hundred words, so this
// only guards against a runaway generation freezing the page.
const MAX_WORDS = 1500;

const words = (s) =>
  String(s ?? "")
    .split(/\s+/)
    .filter(Boolean);

// The model is asked for {"say": "...", "do": [...]}; only `say` is dialogue.
// Returns the dialogue the cleanup step started from (the whole text when the
// reply is not in that shape), so the diff isolates what the cleanup changed
// and is not drowned by the JSON wrapper.
export function dialogueOf(raw) {
  const text = String(raw ?? "").trim();
  const fenced = text.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
  try {
    const obj = JSON.parse(fenced ? fenced[1] : text);
    if (obj && typeof obj === "object" && typeof obj.say === "string")
      return obj.say;
  } catch (e) {
    /* not JSON: the whole reply is the dialogue */
  }
  return text;
}

// Did the cleanup (markdown, emoji, stage directions) alter the dialogue?
export const wasCleaned = (raw, saved) =>
  words(dialogueOf(raw)).join(" ") !== words(saved).join(" ");

// Word diff as HTML: removed words in <del>, added words in <ins>.
export function diffHtml(before, after) {
  const a = words(before);
  const b = words(after);
  if (a.length > MAX_WORDS || b.length > MAX_WORDS) return esc(after);
  const n = a.length;
  const m = b.length;
  const lcs = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      lcs[i][j] =
        a[i] === b[j]
          ? lcs[i + 1][j + 1] + 1
          : Math.max(lcs[i + 1][j], lcs[i][j + 1]);

  const parts = [];
  const push = (kind, word) => {
    const last = parts[parts.length - 1];
    if (last && last.kind === kind) last.words.push(word);
    else parts.push({ kind, words: [word] });
  };
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) (push("same", a[i++]), j++);
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) push("del", a[i++]);
    else push("ins", b[j++]);
  }
  while (i < n) push("del", a[i++]);
  while (j < m) push("ins", b[j++]);

  return parts
    .map((p) => {
      const t = esc(p.words.join(" "));
      return p.kind === "same" ? t : `<${p.kind}>${t}</${p.kind}>`;
    })
    .join(" ");
}

// The comparison block: the model's reply exactly as returned, then what the
// cleanup removed (red) or added (green) on the way to the text that was said
// and saved. `saved` may be null while a reply is still in flight.
export function compareHtml(raw, saved) {
  const rawBlock = `<div class="lbl">Raw response — exactly what the model returned</div><pre class="raw">${esc(raw)}</pre>`;
  if (saved == null) return `<div class="rawcmp">${rawBlock}</div>`;
  const changed = wasCleaned(raw, saved);
  return (
    `<div class="rawcmp">${rawBlock}` +
    `<div class="lbl">${changed ? "Cleanup changed the dialogue (removed / added)" : "Cleanup left the dialogue as it was"}</div>` +
    `<div class="diff">${diffHtml(dialogueOf(raw), saved)}</div></div>`
  );
}
