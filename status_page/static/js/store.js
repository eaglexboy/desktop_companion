// @ts-check
// A page's state, and the pieces that depend on it. `update` changes the state; whatever
// subscribed to it then runs once (several updates in a row are one notification), so a change in
// one place does not have to remember to tell the screen about every place it shows up.
//
//   const page = createStore({ spec: null, loading: false });
//   page.subscribe((state) => { button.disabled = !state.spec; });
//   page.update((s) => { s.spec = chosen; });

/**
 * @template T
 * @param {T} state
 */
export function createStore(state) {
  /** @type {Set<(state: T) => void>} */
  const listeners = new Set();
  let queued = false;

  function notify() {
    queued = false;
    for (const listener of listeners) listener(state);
  }

  return {
    state,

    /** Changes the state in place, then tells the subscribers once. @param {(state: T) => void} change */
    update(change) {
      change(state);
      if (!queued) {
        queued = true;
        queueMicrotask(notify);
      }
    },

    /**
     * Runs `listener` after every update, and once now. Returns how to stop.
     * @param {(state: T) => void} listener
     */
    subscribe(listener) {
      listeners.add(listener);
      listener(state);
      return () => listeners.delete(listener);
    },
  };
}
