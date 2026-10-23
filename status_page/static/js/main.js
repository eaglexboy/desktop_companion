// Entry point: each module registers its own handlers on the bus; this wires
// them up in order and starts the data sources (overview poll, event stream).
import { initTheme } from "./theme.js";
import { initTabs } from "./tabs.js";
import { initPersonality } from "./personality.js";
import { initOverview } from "./overview.js";
import { initSkills } from "./skills.js";
import { initThinking } from "./thinking.js";
import { initTimeline } from "./timeline.js";
import { initLogs } from "./logs.js";
import { connectStream } from "./stream.js";

initTheme();
initPersonality();
initSkills();
initThinking();
initTimeline();
initLogs();
initTabs(); // after the listeners above, so the first "tab" event reaches them
initOverview(); // starts polling; the first result fans out to every panel
connectStream();
