// @ts-check
// "Start from": a button that opens a list with collapsible groups and a filter. What the button
// says follows the state (the chosen value, the looks, the bodies), so nothing has to update it.
import { delegate } from "./dom.js";
import { attrs, html, mount } from "./html.js";
import { filterGroups, startGroups, startText } from "./build-spec.js";
import { S, page } from "./build-state.js";
import { ui } from "./build-ui.js";
import { recall, remember } from "./storage.js";

/** A group the user closed stays closed, until a filter opens them all. @param {string} id */
const collapsedKey = (id) => `build.collapsed.${id}`;

const choices = () => (S.options ? startGroups(S.options.bodies, S.looks) : []);

function renderList() {
  const query = ui.startFilter.value;
  const filtering = query.trim() !== "";
  const groups = filterGroups(choices(), query).map((g) => {
    const group = attrs({
      class: "group",
      "data-group": g.id,
      open: filtering || !recall(collapsedKey(g.id)),
    });
    const items = g.items.map((item) => {
      const tag = attrs({
        type: "button",
        class: `opt${item.value === S.start ? " on" : ""}`,
        role: "option",
        "aria-selected": String(item.value === S.start),
        "data-value": item.value,
      });
      return html`<button ${tag}>${item.text}</button>`;
    });
    return html`<details ${group}><summary>${g.label} <span class="count">${g.items.length}</span></summary>${items}</details>`;
  });
  mount(
    ui.startGroups,
    groups.length
      ? html`${groups}`
      : html`<div class="empty-list">Nothing matches.</div>`,
  );
}

/** @param {boolean} open */
function toggle(open) {
  ui.startPanel.hidden = !open;
  ui.start.setAttribute("aria-expanded", String(open));
  if (open) {
    renderList();
    ui.startFilter.focus();
  } else {
    ui.startFilter.value = ""; // a stale filter would hide the groups' own state
  }
}

/**
 * @param {(value: string) => void} onPick  called with the value chosen ("v:<n>" or "body:<id>")
 */
export function initPicker(onPick) {
  page.subscribe(() => {
    ui.start.textContent = S.unreachable
      ? "Unavailable"
      : startText(choices(), S.start) ||
        (S.options ? "Choose where to start…" : "Loading…");
  });
  ui.start.addEventListener("click", () => {
    if (S.options) toggle(ui.startPanel.hidden);
  });
  ui.startFilter.addEventListener("input", renderList);
  ui.startFilter.addEventListener("keydown", (event) => {
    if (event.key === "Escape") toggle(false);
    if (event.key === "Enter") ui.startGroups.querySelector("button")?.click();
  });
  delegate(ui.startGroups, "click", "[data-value]", (option) => {
    const value = option.dataset.value ?? "";
    page.update((s) => {
      s.start = value;
    });
    toggle(false);
    onPick(value);
  });
  // A group the user closes stays closed (only while not filtering, which opens them all).
  ui.startGroups.addEventListener(
    "toggle",
    (event) => {
      const group = /** @type {HTMLDetailsElement} */ (event.target);
      if (ui.startFilter.value.trim() === "")
        remember(
          collapsedKey(group.dataset.group ?? ""),
          group.open ? "" : "1",
        );
    },
    true,
  );
  document.addEventListener("click", (event) => {
    const inside =
      event.target instanceof Element && event.target.closest(".picker");
    if (!ui.startPanel.hidden && !inside) toggle(false);
  });
}
