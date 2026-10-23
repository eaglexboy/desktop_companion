// Light/dark: follows the OS unless the viewer picked one with the button.
import { $, store } from "./util.js";
import { emit } from "./bus.js";

export function initTheme() {
  const saved = store("theme");
  if (saved) document.documentElement.dataset.theme = saved;
  $("theme").onclick = () => {
    const osDark = matchMedia("(prefers-color-scheme: dark)").matches;
    const current =
      document.documentElement.dataset.theme || (osDark ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store("theme", next);
    emit("theme");
  };
}
