// The Workbench tab (UI v2): program, console and debug on one page.
//
// PHASE 1 STUB (lane UI2-SHELL): today's Program, Consoles and Debug sections, stacked, so
// the tab ships before the Workbench lane replaces this file with the prototype's layout
// (the Program strip with the download bar, the console with Reset DUT, the right rail with
// Debug and Logic analysers). Every drive button keeps its lease gate: the sections are
// today's, unchanged (their ActionRows pass `holder`).
//
// A link to 0.1.0's "program", "consoles" or "debug" lands here with that part scrolled
// into view (route.js OLD_KEYS `part`; ui.js useReveal).

import { html, useRef } from "../lib.js";
import { useReveal } from "../ui.js";
import { ConsolesSection } from "./consoles.js";
import { DebugSection } from "./debug.js";
import { ProgramSection } from "./program.js";

function Part({ bid, part, cls = "", children }) {
  const ref = useRef(null);
  useReveal(bid, part, ref);
  return html`<div class=${`wb-part ${cls}`} ref=${ref} data-testid=${`part-${part}`}>${children}</div>`;
}

export function WorkbenchSection({ bid }) {
  return html`<div class="stack workbench">
    <${Part} bid=${bid} part="program"><${ProgramSection} bid=${bid} /><//>
    <${Part} bid=${bid} part="consoles" cls="wb-consoles"><${ConsolesSection} bid=${bid} /><//>
    <${Part} bid=${bid} part="debug"><${DebugSection} bid=${bid} /><//>
  </div>`;
}
