// The brain's live event stream (relayed by this service). Every event is
// re-published on the bus as "stream"; the header dot shows connection state.
import { $ } from "./util.js";
import { emit } from "./bus.js";

let source = null;
let lastSeq = 0;

const setLive = (cls, text) => {
  $("livedot").className = "dot " + cls;
  $("livetxt").textContent = text;
};

export function connectStream() {
  if (source) source.close();
  source = new EventSource(
    "/api/events" + (lastSeq ? "?since=" + lastSeq : ""),
  );
  source.onopen = () => setLive("ok", "live");
  source.onmessage = (m) => {
    try {
      const ev = JSON.parse(m.data);
      lastSeq = Math.max(lastSeq, ev.seq);
      emit("stream", ev);
    } catch (e) {
      /* a malformed frame is skipped, not fatal */
    }
  };
  source.addEventListener("brain-down", () =>
    setLive("bad", "brain unreachable"),
  );
  source.onerror = () => setLive("warn", "reconnecting…");
}
