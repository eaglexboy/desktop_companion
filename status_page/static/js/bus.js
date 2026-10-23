// A tiny publish/subscribe hub so modules don't import each other.
//
//   "overview"  payload: the /api/overview object, every poll
//   "stream"    payload: one brain event from /api/events
//   "tab"       payload: the tab name that was just shown
//   "theme"     payload: none; the light/dark choice changed
//   "resize"    payload: none

const handlers = new Map();

export function on(name, fn) {
  if (!handlers.has(name)) handlers.set(name, []);
  handlers.get(name).push(fn);
}

export function emit(name, payload) {
  for (const fn of handlers.get(name) || []) {
    try {
      fn(payload);
    } catch (e) {
      console.error(`handler for "${name}" failed`, e); // one bad panel must not blank the others
    }
  }
}
