// Action panels: the rules every button follows, in one place (ported from the Qt
// PanelWidget and the HAPS "panel is data" work).
//
// - The call never blocks the page: it is a promise, and the page keeps rendering.
// - A running button shows its own busy label and elapsed seconds; the panel's other
//   buttons say what they are waiting for.
// - Each action has a time budget. Past it the panel says "still running after N s" and
//   stays usable; the late answer is still shown when it lands.
// - The result box starts with "$ <command>  (rc N, T s)"; a failure adds NAME, message, hint.
// - A capability the board lacks disables the button AND shows the engine's reason.
// - An intrusive action is armed by a tick box, which clears after each run.
// - An interlock that stops a click says so and ends with "Nothing was run."

import { call, toApiError, waitJob } from "./api.js";
import { capState, secs } from "./format.js";
import { boardState, changed, jobLabel, log, S, setJob } from "./store.js";

export const NOTHING_RUN = "Nothing was run.";
export const ARM_REASON = "not armed: tick the arm box first";

export function panelState(bid, panel) {
  const key = `${bid}:${panel}`;
  if (!S.panels[key]) {
    S.panels[key] = { bid, panel, running: null, busyLabel: "", command: "", startedAt: 0,
      budgetS: 30, lines: [], overdue: false };
  }
  return S.panels[key];
}

export function isArmed(bid, armKey) { return !!boardState(bid).arms[armKey]; }

export function setArmed(bid, armKey, on) {
  boardState(bid).arms[armKey] = !!on;
  changed();
}

// Why an action cannot run now, or "" when it can.
export function gateReason(bid, panel, key, opts = {}) {
  const row = S.boards[bid];
  if (!row || !row.open) return "open the board first";
  const b = boardState(bid);
  if (opts.capability) {
    const state = capState(b.info, opts.capability);
    if (!state) return b.infoError ? `Cannot: ${b.infoError.message}` : "waiting for the board's capability view";
    if (!state.available) return `Cannot: ${state.reason}`;
  }
  // The adapter the daemon calls for this action (GET .../session): a link may allow a
  // capability that this build's board session still has no adapter for.
  if (opts.adapter && b.session && b.session.adapters && b.session.adapters[opts.adapter] === false) {
    return `Cannot: this board's session has no ${opts.adapter} adapter in this build`;
  }
  const p = panelState(bid, panel);
  if (p.running) return p.running === key ? "running" : `waiting for ${p.busyLabel}`;
  if (b.job) return `waiting for the ${jobLabel(b.job.kind)} job to finish (socharnessd holds the board)`;
  if (opts.guard) {
    const why = opts.guard();
    if (why) return why;
  }
  if (opts.arm && !isArmed(bid, opts.arm)) return ARM_REASON;
  return "";
}

export function interlock(bid, panel, command, why) {
  const p = panelState(bid, panel);
  p.lines = [
    { kind: "rc", command, notRun: true },
    { kind: "warnline", text: `${why}. ${NOTHING_RUN}` },
  ];
  log("warning", panel, `$ ${command}  (not run): ${why}. ${NOTHING_RUN}`, bid);
  changed();
}

export function errorLines(err) {
  const e = toApiError(err);
  const out = [{ kind: "err", name: e.errName, text: e.message }];
  if (e.holder) out.push({ kind: "hint", text: `holder: ${e.holder}` });
  if (e.hint) out.push({ kind: "hint", text: `hint: ${e.hint}` });
  return out;
}

function rcOf(err) {
  const e = toApiError(err);
  if (e.transport) return null;
  return e.code === null || e.code === undefined ? "?" : e.code;
}

// Start a long operation (202 {job}) and wait for its result. The board is marked busy at
// once, so every other action on it waits (socharnessd would refuse them with HELD).
export async function runJob(name, params, body, onProgress, kind = name) {
  const { data } = await call(name, params, body);
  if (!data.job) return data;
  if (params && params.bid) setJob(params.bid, data.job, kind);
  return waitJob(data.job, { onProgress });
}

// Run one action. spec: {key, command, busyLabel, budgetS, run(ctx), render(result)->lines,
// arm, onDone(ok, value)}. ctx.progress(text) appends a line while it runs.
export async function runAction(bid, panel, spec) {
  const p = panelState(bid, panel);
  if (p.running) return;
  p.running = spec.key;
  p.busyLabel = spec.busyLabel || `${spec.label}...`;
  p.command = spec.command;
  p.startedAt = Date.now();
  p.budgetS = spec.budgetS || 30;
  p.overdue = false;
  const progress = [];
  p.lines = [{ kind: "rc", command: spec.command, running: true }];
  changed();
  // One line per phase: a repeat of the last phase updates its line in place.
  const phases = [];
  const ctx = {
    progress(text, phase = text) {
      const last = phases[phases.length - 1];
      if (last === phase) progress[progress.length - 1] = text;
      else { phases.push(phase); progress.push(text); }
      p.lines = [p.lines[0], ...progress.map((t) => ({ kind: "progress", text: t }))];
      changed();
    },
  };
  const t0 = performance.now();
  let ok = true;
  let value;
  try {
    value = await spec.run(ctx);
  } catch (e) {
    ok = false;
    value = toApiError(e);
  }
  const took = (performance.now() - t0) / 1000;
  const head = { kind: "rc", command: spec.command, rc: ok ? 0 : rcOf(value), secs: took,
    level: ok ? "ok" : "err" };
  const body = ok ? (spec.render ? spec.render(value) : [{ kind: "out", text: "done" }])
    : errorLines(value);
  p.lines = [head, ...progress.map((t) => ({ kind: "progress", text: t })), ...body];
  p.running = null;
  p.overdue = false;
  if (spec.arm) boardState(bid).arms[spec.arm] = false;
  const summary = body.map((l) => (l.name ? `${l.name}: ${l.text}` : l.text)).join("  ");
  const rcText = head.rc === null ? "no answer" : `rc ${head.rc}`;
  log(ok ? "info" : "error", panel, `$ ${spec.command}  (${rcText}, ${secs(took)} s)  ${summary}`, bid);
  if (spec.onDone) {
    try { spec.onDone(ok, value); } catch (e) { /* a render hook never breaks the panel */ }
  }
  changed();
  return { ok, value };
}

export function overdueText(p) {
  const elapsed = (Date.now() - p.startedAt) / 1000;
  return `still running after ${secs(elapsed)} s (budget ${secs(p.budgetS)} s): the daemon has ` +
    "not answered yet. The page stays usable; the answer lands here when it does.";
}

// Budgets: one ticker for every running panel.
setInterval(() => {
  let any = false;
  for (const p of Object.values(S.panels)) {
    if (!p.running) continue;
    any = true;
    const elapsed = (Date.now() - p.startedAt) / 1000;
    if (!p.overdue && elapsed > p.budgetS) {
      p.overdue = true;
      log("warning", p.panel, `${p.command}: ${overdueText(p)}`, p.bid);
    }
  }
  // A job another client started (the CLI, another page) also shows its elapsed time.
  if (any || Object.values(S.board).some((b) => b.job)) {
    S.tick += 1;
    changed();
  }
}, 1000);
