// Workbench rail card "DUT clock" (lane L5c COSIM; net-protocol v0.20 `cosim`): the DUT's
// cycle counter, a state chip, and Halt / Step N / Run. The card is offered only on a board
// whose harness reports the engine feature `cosim` (the feature NAME, never the protocol
// version). Reading the count is open; Halt, Step and Run need the board claimed by you, so
// they are disabled with the reason until it is. Daemon: GET/POST /boards/{bid}/cosim
// (daemon/cosim_api.py).
//
// The lead wires it (this lane owns this file only):
//   1. api.js ENDPOINTS: add COSIM_ENDPOINTS (below) -- `Object.assign`-style, two lines.
//   2. workbench.js Rail(): `${cosimOffered(bid) ? html`<${CosimCard} bid=${bid} />` : null}`
//      after <${XvcCard}/>; the folded strip may skip it.

import { call } from "../api.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed } from "../store.js";
import { Card, Chip, Icon, Reason, Spinner } from "../ui.js";
import { featuresOf } from "./boardfacts.js";
import { claimOf } from "./claim.js";

// Add these two entries to api.js's ENDPOINTS.
export const COSIM_ENDPOINTS = Object.freeze({
  cosimState: ["GET", "/boards/{bid}/cosim"],
  cosimAct: ["POST", "/boards/{bid}/cosim"],
});

export const FEATURE = "cosim";
export const STEP_MIN = 1;
export const STEP_MAX = 100000000;
export const DEFAULT_STEP = 10000;
export const POLL_MS = 5000;

export const LOCKED_WHY = "Claim the board to stop its clock";
export const NO_CLOCK = "This board's image has no clock control";
export const LIMITATION = "Serial input and the network receive are not held while the clock is stopped, "
  + "so a stopped or stepped run is exact inside the DUT and on its buses, but not for those pins.";

const V = new Map();                      // bid -> card state

function cardState(bid) {
  let s = V.get(bid);
  if (!s) {
    s = { data: null, busy: "", error: null, steps: String(DEFAULT_STEP), loaded: false, timer: 0 };
    V.set(bid, s);
  }
  return s;
}

// Offered only when the harness says it has the feature (by name).
export function cosimOffered(bid) {
  return featuresOf(boardState(bid)).includes(FEATURE);
}

// A whole number of cycles in range, else a sentence; "" when fine.
export function stepProblem(text) {
  const t = String(text).trim().replace(/[,_ ]/g, "");
  if (!/^\d+$/.test(t)) return `Steps must be a whole number from ${STEP_MIN} to ${STEP_MAX.toLocaleString("en-US")}`;
  const n = Number(t);
  if (n < STEP_MIN || n > STEP_MAX) return `Steps must be from ${STEP_MIN} to ${STEP_MAX.toLocaleString("en-US")}`;
  return "";
}

export function stepCount(text) { return Number(String(text).trim().replace(/[,_ ]/g, "")); }

// Why Halt / Step / Run are off, or "".
export function whyOff(b, busy = "") {
  if (!featuresOf(b).includes(FEATURE)) return NO_CLOCK;
  const c = claimOf(b);
  if (!c || c.state !== "mine") return LOCKED_WHY;
  if (busy) return "Waiting for the board";
  return "";
}

export function cyclesText(n) {
  return n === null || n === undefined ? "–" : Number(n).toLocaleString("en-US");
}

function chipOf(d) {
  if (!d) return { level: "unk", icon: "circle-help", text: "Unknown" };
  return d.running ? { level: "ok", icon: "play", text: "Running" }
    : { level: "warn", icon: "circle-pause", text: "Stopped" };
}

async function read(bid, quiet) {
  const s = cardState(bid);
  try {
    const r = await call("cosimState", { bid }, undefined, null, quiet ? { background: true } : {});
    const d = r.data;
    if (d && d.quiet) return;                         // the daemon held a background read back
    s.data = d && d.offered ? d : null;
    s.reason = d && !d.offered ? d.reason : "";
    s.error = null;
  } catch (e) {
    if (!quiet) s.error = e;
  }
  s.loaded = true;
  changed();
}

async function act(bid, what, n) {
  const s = cardState(bid);
  s.busy = what;
  s.error = null;
  changed();
  try {
    const r = await call("cosimAct", { bid }, what === "step" ? { act: what, n } : { act: what });
    s.data = { ...(s.data || {}), ...r.data, offered: true };
  } catch (e) {
    s.error = e;
  }
  s.busy = "";
  changed();
}

export function CosimCard({ bid }) {
  const b = boardState(bid);
  const s = cardState(bid);
  const offered = cosimOffered(bid);
  useEffect(() => {
    if (!offered) return undefined;
    if (!s.loaded) read(bid, false);
    s.timer = setInterval(() => { if (!document.hidden && !s.busy) read(bid, true); }, POLL_MS);
    return () => clearInterval(s.timer);
  }, [bid, offered]);
  if (!offered) return null;
  const d = s.data;
  const chip = chipOf(d);
  const why = whyOff(b, s.busy);
  const problem = stepProblem(s.steps);
  const off = !!why;
  const again = html`<button type="button" class="btn ghost sm" data-action="cosim-read" onClick=${() => read(bid, false)}
    aria-busy=${s.busy ? "true" : undefined}><${Icon} name="refresh-cw" /> Read</button>`;
  return html`<${Card} title="DUT clock" icon="timer" testid="cosim-card" actions=${again}
      sub="Stop the DUT's clock, run it for an exact number of cycles, or let it run again.">
    <div class="stack gap-8" data-state=${d ? (d.running ? "run" : "halt") : "unknown"}>
      <div class="row gap-8">
        <span class="num mono" style="font-size:20px" data-testid="cosim-cycles"
          title="DUT clock cycles counted so far">${cyclesText(d && d.cycles)}</span>
        <span class="muted small">cycles</span>
        <span class="spacer"></span>
        <${Chip} level=${chip.level} icon=${chip.icon} testid="cosim-state">${chip.text}<//>
      </div>
      <div class="row gap-8">
        <button type="button" class="btn sm" data-action="cosim-halt" disabled=${off || (d && !d.running)}
          title=${why || "Stop the DUT's clock"} onClick=${() => act(bid, "halt")}>
          ${s.busy === "halt" ? html`<${Spinner} />` : html`<${Icon} name="square" />`} Halt</button>
        <button type="button" class="btn sm" data-action="cosim-run" disabled=${off || (d && d.running)}
          title=${why || "Let the DUT's clock run"} onClick=${() => act(bid, "run")}>
          ${s.busy === "run" ? html`<${Spinner} />` : html`<${Icon} name="play" />`} Run</button>
      </div>
      <div class="row gap-8">
        <button type="button" class="btn sm" data-action="cosim-step" disabled=${off || !!problem}
          title=${why || problem || "Run exactly this many cycles, then stop"}
          onClick=${() => act(bid, "step", stepCount(s.steps))}>
          ${s.busy === "step" ? html`<${Spinner} />` : html`<${Icon} name="chevron-right" />`} Step</button>
        <input type="text" inputmode="numeric" class="input mono num" style="width:9em" data-testid="cosim-steps"
          aria-label="Cycles to step" value=${s.steps} disabled=${off}
          onInput=${(e) => { s.steps = e.target.value; changed(); }} />
        <span class="muted small">cycles</span>
      </div>
      ${why && why !== "Waiting for the board" ? html`<${Reason} testid="cosim-why" text=${why} />` : null}
      ${s.steps && problem ? html`<${Reason} level="warn" text=${problem} />` : null}
      ${s.error ? html`<${Reason} level="err" testid="cosim-error" text=${s.error.message} />` : null}
      <p class="muted small" data-testid="cosim-limit">${LIMITATION}</p>
    </div>
  <//>`;
}

// The mount function the lead wires into the rail.
export function mountCosim(bid) { return cosimOffered(bid) ? html`<${CosimCard} bid=${bid} />` : null; }
