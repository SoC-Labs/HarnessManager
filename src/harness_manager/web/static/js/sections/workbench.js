// The Workbench tab (UI v2, docs/design/ui-v2/prototype-b-round3.html "Workbench"; plan §1.3):
// the bring-up loop on one page. Program strip on top (the picker, the inline preflight,
// Arm -> Program, Restore baseline, the Card line, one outcome box that is the download bar
// while a deploy runs); the console under it, with Reset DUT in its toolbar; the right rail
// holds Debug and Logic analysers ONLY (the front panel lives on the Overview), and what height
// they leave shows "Recent here". Below 1280 px the rail folds to icons with flyouts.
//
// The parts keep 0.1.0's links: `part-program`, `part-consoles`, `part-debug` (route.js OLD_KEYS
// `part`; tests/web/nav.py). A link to "debug" on a folded rail opens the Debug flyout.

import { html, useEffect, useState } from "../lib.js";
import { registerTab } from "../route.js";
import { boardState, changed, openActivity, S } from "../store.js";
import { Icon } from "../ui.js";
import { ConsolesSection } from "./consoles.js";
import { DebugCard, debugLive } from "./debug.js";
import { ProgramStrip } from "./program.js";
import { viewState, XvcCard } from "./xvc.js";

// The rail folds below this width (the window's), unless the user folded or unfolded it.
export const FOLD_BELOW = 1280;
const W = { fold: null, flyout: null };          // fold: null (by width) | true | false

function useWidth() {
  const [w, setW] = useState(typeof window !== "undefined" ? window.innerWidth : 1440);
  useEffect(() => {
    const on = () => setW(window.innerWidth);
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, []);
  return w;
}

// --- Recent here: this board's last events, in the height the two rail cards leave ------------

const LEVEL_ICON = { ok: "circle-check", info: "info", warning: "triangle-alert", error: "circle-x" };
const LEVEL_CLS = { ok: "ok", info: "info", warning: "warn", error: "err" };

function hhmm(at) {
  const d = new Date((at || Date.now() / 1000) * 1000);
  return d.toTimeString().slice(0, 5);
}

function WbRecent({ bid }) {
  const rows = [];
  for (let i = S.log.length - 1; i >= 0 && rows.length < 14; i -= 1) {
    const e = S.log[i];
    if (e.board === bid && e.source !== "job.progress") rows.push(e);
  }
  return html`<div class="wb-fill"><section class="card wb-recent" aria-label=${`Recent on ${bid}`} data-testid="wb-recent">
    <div class="card-head"><h2 class="card-title"><${Icon} name="history" />Recent here</h2><span class="spacer"></span>
      <button type="button" class="btn ghost sm" data-action="wb-activity" title="Activity, filtered to this board"
        onClick=${() => openActivity(bid)}>All activity</button></div>
    <ul class="wb-recent-list">${rows.length ? rows.map((r) => html`<li key=${r.id} class=${`lv-${LEVEL_CLS[r.level] || "info"}`}
        title=${`${hhmm(r.at)} · ${r.source} · ${r.text}`}>
        <span class="t">${hhmm(r.at)}</span><${Icon} name=${LEVEL_ICON[r.level] || "info"} /><span class="m">${/\./.test(r.source) ? `${r.source}: ` : ""}${r.text || r.source}</span></li>`)
      : html`<li class="none">Nothing yet on this board since this page opened.</li>`}</ul>
  </section></div>`;
}

// --- the rail ----------------------------------------------------------------------------------

function Rail({ bid }) {
  return html`<aside class="wb-rail" aria-label="Debug and logic analysers" data-testid="part-debug">
    <div class="rail-fold-head"><span>Debug · ILA</span>
      <button type="button" class="btn ghost sm" data-action="wb-fold" title="Fold the rail to icons"
        onClick=${() => { W.fold = true; W.flyout = null; changed(); }}><${Icon} name="panel-right" />Fold</button></div>
    <${DebugCard} bid=${bid} />
    <${XvcCard} bid=${bid} />
    <${WbRecent} bid=${bid} />
  </aside>`;
}

function Folded({ bid }) {
  const items = [
    { k: "debug", icon: "bug", l: "Debug", dot: debugLive(bid) ? "ok" : "" },
    { k: "ila", icon: "scan-search", l: "ILA", dot: ["ready", "attached"].includes(viewState(bid)) ? "ok" : viewState(bid) === "held" ? "held" : "" },
  ];
  const fly = W.flyout;
  return html`<div class="flyout-wrap">
    <div class="fold-strip" role="toolbar" aria-label="Debug and logic analysers">
      <button type="button" class="fold-btn" data-action="wb-unfold" title="Unfold the rail" aria-label="Unfold the rail"
        onClick=${() => { W.fold = false; W.flyout = null; changed(); }}><${Icon} name="panel-right" /></button>
      <div class="fold-sep"></div>
      ${items.map((it) => html`<button type="button" key=${it.k} class="fold-btn" data-fly=${it.k}
          aria-pressed=${fly === it.k ? "true" : "false"} title=${it.l} aria-label=${it.l}
          onClick=${() => { W.flyout = fly === it.k ? null : it.k; changed(); }}><${Icon} name=${it.icon} />
          ${it.dot ? html`<span class=${`dot ${it.dot}`}></span>` : null}</button>
        <div class="fold-lbl" key=${`l${it.k}`}>${it.l}</div>`)}
    </div>
    ${fly ? html`<div class="flyout" role="dialog" aria-label=${fly === "debug" ? "Debug" : "Logic analysers"} data-testid="part-debug">
      <div class="flyout-close"><button type="button" class="btn ghost sm icon-only" title="Close" aria-label="Close the flyout"
        onClick=${() => { W.flyout = null; changed(); }}><${Icon} name="x" /></button></div>
      ${fly === "debug" ? html`<${DebugCard} bid=${bid} />` : html`<${XvcCard} bid=${bid} />`}
    </div>` : null}
  </div>`;
}

export function WorkbenchSection({ bid }) {
  const width = useWidth();
  const r = S.ui.reveal;
  const revealDebug = !!(r && r.bid === bid && r.part === "debug");
  const revealAt = revealDebug ? r.at : 0;
  const folded = width <= 760 ? false : W.fold === null ? width < FOLD_BELOW : W.fold;
  // A link to 0.1.0's "debug" on a folded rail opens the Debug flyout.
  useEffect(() => {
    if (revealAt && folded && !W.flyout) { W.flyout = "debug"; changed(); }
  }, [revealAt]);
  boardState(bid);                       // the board's state exists before its parts read it
  return html`<div class=${`wb ${folded ? "folded" : ""}`} data-testid="workbench" data-folded=${folded ? "true" : "false"}>
    <div class="wb-part wb-strip-part" data-testid="part-program"><${ProgramStrip} bid=${bid} /></div>
    <div class="wb-main wb-part" data-testid="part-consoles"><${ConsolesSection} bid=${bid} /></div>
    ${folded ? html`<${Folded} bid=${bid} />` : html`<${Rail} bid=${bid} />`}
  </div>`;
}

// A fill tab: the console takes the height the strip leaves (app.js section-body "fill").
registerTab("workbench", WorkbenchSection, { fill: true });
