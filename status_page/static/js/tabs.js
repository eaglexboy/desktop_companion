// Top-level tabs, driven by the location hash so a tab is linkable.
import { $ } from "./util.js";
import { emit } from "./bus.js";

export const TABS = [
  "overview",
  "thinking",
  "skills",
  "timeline",
  "chat",
  "logs",
];

export function showTab(name) {
  if (!TABS.includes(name)) name = "overview";
  TABS.forEach((t) => $("tab-" + t).classList.toggle("on", t === name));
  document
    .querySelectorAll("#nav button")
    .forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === name));
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  emit("tab", name);
}

export function initTabs() {
  document
    .querySelectorAll("#nav button")
    .forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
  addEventListener("hashchange", () => showTab(location.hash.slice(1)));
  showTab(location.hash.slice(1));
}

export const isShown = (name) => $("tab-" + name).classList.contains("on");
