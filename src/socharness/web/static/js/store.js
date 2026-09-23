// Application state, the event router, and the reads the UI makes.
//
// One mutable state object; every change calls changed(), which re-renders the app on
// the next animation frame. The app is small, so a whole-tree Preact diff is cheaper
// than bookkeeping, and nothing can show a stale copy.

import {
  call, EventSocket, heldByJob, jobEvent, onConnection, toApiError, unwrapDebug, unwrapInfo,
} from "./api.js";
import { clock, secs } from "./format.js";

export const UI_NOTE = "socharness-ui";
const LOG_MAX = 2000;

export const S = {
  connection: "unknown",     // unknown | ok | down | auth
  eventsUp: false,
  version: "",
  packs: {},
  boards: {},                // board_id -> {board_id, open, holder, candidate}
  order: [],                 // board ids in rail order
  scan: { running: false, line: "", level: "" },
  selected: null,
  sections: {},              // board_id -> section key
  theme: "system",
  board: {},                 // board_id -> per-board data (see boardState)
  panels: {},                // "board_id:panel" -> action panel state (see actions.js)
  log: [],
  logSeq: 0,
  tick: 0,
};

// --- change notification -----------------------------------------------------------------

const listeners = new Set();
let scheduled = false;

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function changed() {
  if (scheduled) return;
  scheduled = true;
  const run = () => {
    scheduled = false;
    for (const fn of listeners) fn();
  };
  if (typeof requestAnimationFrame === "function" && document.visibilityState !== "hidden") {
    requestAnimationFrame(run);
  } else {
    setTimeout(run, 0);
  }
}

// --- per-board state ---------------------------------------------------------------------

export function boardState(bid) {
  if (!S.board[bid]) {
    S.board[bid] = {
      info: null, infoError: null, infoLine: "", infoLoading: false,
      telemetry: null, telemetryError: null, telemetryLine: "", telemetryLoading: false,
      overlays: null, overlaysError: null, overlaysLine: "", overlaysLoading: false,
      selectedOverlay: null, preflight: null, preflightFor: null, preflightError: null,
      preflightLine: "", preflightLoading: false, preflightGen: 0,
      deploy: { state: "idle", overlay: "", phase: "", bytes: 0, total: 0, phases: [],
        events: [], verified: false, rm_id: "", seconds: 0, transport: "", reason: "",
        stage: "" },
      debug: null, idcode: "",
      pending: undefined, pendingSeen: false,
      consoles: null, consolesError: null, consolesLine: "", consoleSelected: null,
      reboot: { phases: [] },
      backup: null,
      arms: {},
      job: null,             // {id, kind, at}: a job socharnessd is running on this board
      session: null,         // GET /boards/{bid}/session: {adapters, reset_targets, job}
      deferred: false,       // a read was refused by the job; read again when it ends
    };
  }
  return S.board[bid];
}

export function sectionOf(bid) { return S.sections[bid] || "overview"; }

export function setSection(bid, section) {
  S.sections[bid] = section;
  try { window.sessionStorage.setItem("socharness.sections", JSON.stringify(S.sections)); }
  catch (e) { /* not remembered */ }
  changed();
}

export function select(bid) {
  S.selected = bid;
  try { window.sessionStorage.setItem("socharness.selected", bid || ""); } catch (e) { /* ok */ }
  changed();
  const row = S.boards[bid];
  if (row && row.open) openedBoard(bid);
}

export function restoreSelection() {
  try {
    const sections = JSON.parse(window.sessionStorage.getItem("socharness.sections") || "{}");
    if (sections && typeof sections === "object") S.sections = sections;
    return window.sessionStorage.getItem("socharness.selected") || null;
  } catch (e) {
    return null;
  }
}

// --- jobs: while one runs, socharnessd refuses every other request on that board ------------

const JOB_LABELS = {
  deploy: "deploy", restore: "restore", debug_up: "debug session start", reboot: "board reboot",
  sd_backup: "SD backup", sd_install: "SD install", sd_restore: "SD restore",
};

export function jobLabel(kind) {
  return JOB_LABELS[kind] || String(kind || "job").replace(/[_.]/g, " ");
}

export function setJob(bid, id, kind) {
  if (endedJobs.has(id)) return;          // its end already arrived: never resurrect it
  const b = boardState(bid);
  if (b.job && b.job.id === id) {
    if (kind && b.job.kind === "job") b.job.kind = kind;
    return;
  }
  b.job = { id, kind: kind || "job", at: Date.now() };
  if (!kind && id && id !== "?") learnJob(bid, id);
  changed();
}

// A job learned of without its kind (GET /boards, a missed job.started): ask the daemon.
async function learnJob(bid, id) {
  try {
    const { data } = await call("job", { id });
    const b = boardState(bid);
    if (b.job && b.job.id === id && data.kind) {
      b.job.kind = data.kind;
      if (data.started_at) b.job.at = data.started_at * 1000;
      changed();
    }
    if (data.state && data.state !== "running") jobEnded(bid, id);
  } catch (e) { /* the chip still says a job runs; job.done or GET /boards ends it */ }
}

const endedJobs = new Set();

// The job ended: the board is free again, so read what it changed.
function jobEnded(bid, id) {
  if (id) {
    endedJobs.add(id);
    if (endedJobs.size > 500) endedJobs.delete(endedJobs.values().next().value);
  }
  const b = boardState(bid);
  if (!b.job || b.job.id !== id) return;
  const kind = b.job.kind;
  b.job = null;
  b.deferred = false;
  refreshInfo(bid);
  if (kind === "deploy" || kind === "restore") {
    loadOverlays(bid);
    if (b.selectedOverlay) runPreflight(bid, b.selectedOverlay);
  }
  if (kind === "debug_up") loadDebug(bid);
  if (kind === "reboot" || kind.startsWith("sd_")) loadPending(bid);
  changed();
}

// A read refused because a job holds the board is not an error: wait for the job.
// socharnessd names it in the holder: "socharnessd <kind> job <id>".
function deferIfHeld(bid, err) {
  if (!heldByJob(err)) return false;
  const b = boardState(bid);
  b.deferred = true;
  const named = /(\S+) job (\S+)/.exec(err.holder || "");
  if (named) setJob(bid, named[2], named[1]);
  else if (!b.job) b.job = { id: "?", kind: "job", at: Date.now() };
  changed();
  return true;
}

// --- the activity log --------------------------------------------------------------------

export function log(level, source, text, board = "", at = null) {
  S.logSeq += 1;
  S.log.push({ id: S.logSeq, at: at || Date.now() / 1000, level, source, text, board });
  if (S.log.length > LOG_MAX) S.log.splice(0, S.log.length - LOG_MAX);
  changed();
}

// --- timed reads ------------------------------------------------------------------------

// A read with its "$ what (rc N, T s)" line. Returns {data} or {error}.
export async function timed(what, fn) {
  const t0 = performance.now();
  try {
    const data = await fn();
    const line = `$ ${what}  (rc 0, ${secs((performance.now() - t0) / 1000)} s)`;
    return { data, line, error: null };
  } catch (e) {
    const err = toApiError(e);
    const rc = err.code === null || err.code === undefined ? "?" : err.code;
    const line = err.transport
      ? `$ ${what}  (no answer, ${secs((performance.now() - t0) / 1000)} s)`
      : `$ ${what}  (rc ${rc}, ${secs((performance.now() - t0) / 1000)} s)`;
    return { data: null, line, error: err };
  }
}

// --- boards --------------------------------------------------------------------------------

function mergeBoards(rows) {
  for (const row of rows) {
    const prev = S.boards[row.board_id] || {};
    S.boards[row.board_id] = { ...prev, ...row };
    if (!S.order.includes(row.board_id)) S.order.push(row.board_id);
  }
}

export async function loadBoards() {
  const r = await timed("boards", () => call("boards"));
  if (r.error) return r;
  const rows = r.data.data.boards || [];
  const seen = new Set(rows.map((b) => b.board_id));
  for (const bid of Object.keys(S.boards)) {
    // A board this page knows but the daemon no longer lists stays, marked not open.
    if (!seen.has(bid)) S.boards[bid] = { ...S.boards[bid], open: false };
  }
  mergeBoards(rows);
  for (const row of rows) {
    if (row.job) setJob(row.board_id, row.job, null);
    else if (S.board[row.board_id] && S.board[row.board_id].job && row.open) {
      // The list says no job: one we missed the end of (the event socket was down).
      jobEnded(row.board_id, S.board[row.board_id].job.id);
    }
    if (row.open) openedBoard(row.board_id, { quiet: true });
  }
  changed();
  return r;
}

// Probes run one after another: an address added while a scan runs waits its turn
// instead of being dropped.
let probeChain = Promise.resolve();

export function probe(hosts = null) {
  probeChain = probeChain.then(() => probeNow(hosts), () => probeNow(hosts));
  return probeChain;
}

async function probeNow(hosts) {
  S.scan = { running: true, line: hosts ? `adding ${hosts.join(", ")}...` : "scanning...", level: "" };
  changed();
  // With explicit hosts the pack pings only those and unicasts identify to them, so a board
  // in stage0 rescue (identify, no 6900) is still found: keep scan_network on.
  const body = hosts ? { hosts, scan_usb: false } : {};
  const what = hosts ? `probe ${hosts.join(" ")}` : "probe";
  const r = await timed(what, () => call("probe", {}, body));
  if (r.error) {
    S.scan = { running: false, line: `${r.line}\n${r.error.errName}: ${r.error.message}`, level: "err" };
    log("error", "probe", `${r.line}  ${r.error.message}`);
    changed();
    return;
  }
  const cands = r.data.data.candidates || [];
  mergeBoards(cands.map((c) => ({ board_id: c.board_id, candidate: c })));
  await loadBoards();
  const n = cands.length;
  S.scan = {
    running: false,
    line: n ? `${r.line}: ${n} board${n === 1 ? "" : "s"}`
      : `${r.line}: no boards answered. Check the Ethernet link, or add one by address.`,
    level: n ? "" : "warn",
  };
  log(n ? "info" : "warning", "probe", S.scan.line);
  if (!S.selected && S.order.length) select(S.order[0]);
  if (hosts && cands.length) select(cands[0].board_id);
  changed();
}

// After a board is open in the daemon: read everything the workspace shows.
export function openedBoard(bid, { quiet = false } = {}) {
  const b = boardState(bid);
  if (b.readOnOpen) return;
  b.readOnOpen = true;
  refreshInfo(bid);
  loadSession(bid);
  loadPending(bid);
  loadDebug(bid);
  if (!quiet) changed();
}

export async function refreshInfo(bid) {
  const b = boardState(bid);
  b.infoGen = (b.infoGen || 0) + 1;
  const gen = b.infoGen;
  b.infoLoading = true;
  changed();
  const r = await timed("info", () => call("info", { bid }));
  if (gen !== b.infoGen) return;          // a newer read is on its way: it wins
  b.infoLoading = false;
  if (r.error && deferIfHeld(bid, r.error)) return;
  b.infoLine = r.line;
  if (r.error) {
    b.infoError = r.error;
    if (r.error.errName === "ABSENT") {
      // Not open here any more (closed from the CLI, or the daemon restarted).
      b.info = null;
      if (S.boards[bid]) S.boards[bid].open = false;
    }
  } else {
    b.info = unwrapInfo(r.data.data);
    b.infoError = null;
    b.infoOkAt = Date.now() / 1000;
    if (!b.telemetry && !b.telemetryLoading) loadTelemetry(bid);
  }
  changed();
}

const refreshTimers = {};
export function scheduleRefresh(bid, ms = 250) {
  clearTimeout(refreshTimers[bid]);
  refreshTimers[bid] = setTimeout(() => {
    if (!S.boards[bid] || !S.boards[bid].open) return;
    if (S.board[bid] && S.board[bid].job) S.board[bid].deferred = true;   // after the job
    else refreshInfo(bid);
  }, ms);
}

export async function loadTelemetry(bid) {
  const b = boardState(bid);
  if (b.telemetryLoading) return;
  b.telemetryLoading = true;
  changed();
  const r = await timed("telemetry", () => call("telemetry", { bid }));
  b.telemetryLoading = false;
  if (r.error && deferIfHeld(bid, r.error)) return;
  b.telemetryLine = r.line;
  b.telemetryError = r.error;
  if (!r.error) b.telemetry = r.data.data.readings || [];
  changed();
}

export async function loadOverlays(bid) {
  const b = boardState(bid);
  if (b.overlaysLoading) return;
  b.overlaysLoading = true;
  changed();
  const r = await timed("overlays", () => call("overlays", { bid }));
  b.overlaysLoading = false;
  if (r.error && deferIfHeld(bid, r.error)) return;
  b.overlaysLine = r.line;
  b.overlaysError = r.error;
  if (!r.error) {
    const d = r.data.data;
    b.overlays = { loadable: d.loadable || [], blocked: d.blocked || {}, all: d.overlays || null };
  }
  changed();
}

export async function runPreflight(bid, name) {
  const b = boardState(bid);
  b.preflightGen += 1;
  const gen = b.preflightGen;
  b.preflight = null;
  b.preflightFor = name;
  b.preflightError = null;
  b.preflightLoading = true;
  b.preflightLine = `$ preflight ${name}  (running)`;
  changed();
  const o = b.overlays;
  const ref = o && ((o.loadable || []).find((x) => x.name === name) || (o.all || []).find((x) => x.name === name));
  const r = await timed(`preflight ${name}`, () => call("preflight", { bid }, { overlay: ref || name }));
  if (gen !== b.preflightGen) return;      // a newer selection superseded this one
  b.preflightLoading = false;
  if (r.error && deferIfHeld(bid, r.error)) {
    b.preflightLine = `$ preflight ${name}  (waits for the running job)`;
    return;
  }
  b.preflightLine = r.line;
  b.preflightError = r.error;
  if (!r.error) b.preflight = { items: r.data.data.items || [], refusal: r.data.data.refusal || null };
  changed();
}

export async function loadDebug(bid) {
  const b = boardState(bid);
  const asked = performance.now();
  const r = await timed("debug status", () => call("debugStatus", { bid }));
  if ((b.debugAt || 0) > asked) return;   // the state changed while this read was out
  if (!r.error) b.debug = unwrapDebug(r.data.data);
  else if (!b.debug) b.debug = { state: "unknown", detail: r.error.message };
  changed();
}

export async function loadSession(bid) {
  const b = boardState(bid);
  const r = await timed("session", () => call("session", { bid }));
  if (r.error) return;          // an older daemon without the route: capabilities still gate
  b.session = r.data.data;
  if (b.session.job) setJob(bid, b.session.job, null);
  changed();
}

export async function loadPending(bid) {
  const b = boardState(bid);
  const r = await timed("sd pending", () => call("sdPending", { bid }));
  if (r.error) {
    // No storage adapter (no Debug USB) is normal: there is nothing to recover then.
    b.pending = null;
    changed();
    return;
  }
  const pending = r.data.data.pending || null;
  const first = pending && !b.pendingSeen;
  b.pending = pending;
  if (first) {
    b.pendingSeen = true;
    S.sections[bid] = "power";          // recovery before anything else
    log("error", "storage", "Interrupted SD install: restore it first (Reset & Power)", bid);
  }
  changed();
}

export async function loadConsoles(bid) {
  const b = boardState(bid);
  const r = await timed("consoles", () => call("consoles", { bid }));
  b.consolesLine = r.line;
  b.consolesError = r.error;
  if (!r.error) {
    b.consoles = r.data.data.names || [];
    if (!b.consoleSelected || !b.consoles.includes(b.consoleSelected)) {
      b.consoleSelected = b.consoles[0] || null;
    }
  }
  changed();
}

// --- the event router ----------------------------------------------------------------------

const REFRESH_TOPICS = new Set([
  "board.identity", "session.opened", "session.closed", "deploy.done", "deploy.failed",
]);

function eventLevel(ev) {
  const d = ev.data || {};
  if (ev.topic.endsWith(".failed") || d.state === "failed") return "error";
  if (ev.topic === "deploy.done") return d.verified ? "ok" : "warning";
  if (ev.topic === "board.lost" || ev.topic === "session.closed" || d.state === "down") return "warning";
  return "info";
}

function valueSummary(v) {
  if (Array.isArray(v)) {
    if (v.every((x) => x && typeof x === "object" && x.kind)) return v.map((x) => x.kind).join(",");
    return v.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join(",");
  }
  if (v && typeof v === "object") {
    const text = JSON.stringify(v);
    return text.length > 160 ? `${text.slice(0, 157)}...` : text;
  }
  return String(v);
}

function eventText(ev) {
  const d = ev.data || {};
  const parts = Object.entries(d)
    .filter(([k]) => k !== "preflight")
    .map(([k, v]) => `${k}=${valueSummary(v)}`);
  return parts.length ? parts.join("  ") : "";
}

function onDeployEvent(ev) {
  const b = boardState(ev.board_id);
  const d = ev.data || {};
  const dep = b.deploy;
  if (ev.topic === "deploy.started") {
    Object.assign(dep, { state: "running", overlay: d.overlay || d.rm_id || "", phase: "started",
      bytes: 0, total: 0, phases: [], events: [], verified: false, reason: "", stage: "",
      rm_id: d.rm_id || "" });
    dep.events.push(`${clock(ev.at)}  started ${dep.overlay}`);
  } else if (ev.topic === "deploy.progress") {
    if (dep.state !== "running") Object.assign(dep, { state: "running", phases: [], events: [] });
    dep.phase = d.phase || "?";
    dep.bytes = Number(d.bytes || 0);
    dep.total = Number(d.total || 0);
    if (!dep.phases.includes(dep.phase)) dep.phases.push(dep.phase);
    dep.events.push(`${clock(ev.at)}  ${dep.phase} ${dep.bytes}/${dep.total}`);
  } else if (ev.topic === "deploy.done") {
    Object.assign(dep, { state: "done", phase: "done", verified: !!d.verified,
      rm_id: d.rm_id || "", seconds: Number(d.seconds || 0), transport: d.transport || "" });
    if (dep.total) dep.bytes = dep.total;
    dep.events.push(`${clock(ev.at)}  done rm_id ${d.rm_id} verified=${d.verified ? "yes" : "no"}`);
  } else if (ev.topic === "deploy.failed") {
    Object.assign(dep, { state: "failed", reason: d.reason || "no reason given",
      stage: d.stage || "" });
    if (d.overlay) dep.overlay = d.overlay;
    dep.events.push(`${clock(ev.at)}  failed: ${dep.reason}`);
  }
  if (dep.events.length > 200) dep.events.splice(0, dep.events.length - 200);
}

export function handleEvent(ev) {
  const bid = ev.board_id || "";
  if (ev.topic.startsWith("job.")) jobEvent(ev);
  if (ev.topic === "events.dropped") {
    // The page fell behind and the daemon dropped events: read the state again.
    log("warning", "events", `the daemon dropped ${(ev.data || {}).dropped} events for this page; reading again`);
    loadBoards();
    for (const [id, row] of Object.entries(S.boards)) if (row.open) scheduleRefresh(id, 50);
    return;
  }
  if (bid && ev.topic === "job.started") setJob(bid, (ev.data || {}).job, (ev.data || {}).kind);
  if (bid && (ev.topic === "job.done" || ev.topic === "job.failed")) jobEnded(bid, (ev.data || {}).job);
  if (ev.topic !== "job.progress") {
    log(eventLevel(ev), ev.topic, eventText(ev), bid, ev.at);
  }
  if (!bid) return;
  if (ev.topic.startsWith("deploy.")) onDeployEvent(ev);
  if (ev.topic === "debug.state") {
    const b = boardState(bid);
    b.debugAt = performance.now();
    const d = ev.data || {};
    const ports = d.ports || {};
    b.debug = { ...(b.debug || {}), state: d.state || "unknown",
      gdb_port: ports.gdb || (d.state === "up" ? (b.debug || {}).gdb_port : 0) || 0,
      telnet_port: ports.telnet || (d.state === "up" ? (b.debug || {}).telnet_port : 0) || 0,
      tcl_port: ports.tcl || (d.state === "up" ? (b.debug || {}).tcl_port : 0) || 0 };
    if (d.pid !== undefined) b.debug.pid = d.pid;
    if (d.detail !== undefined) b.debug.detail = d.detail;
    if (d.config !== undefined) b.debug.config = d.config;
    if (d.state !== "up" && d.state !== "starting") {
      b.debug.gdb_port = 0; b.debug.telnet_port = 0; b.debug.tcl_port = 0;
    }
  }
  if (ev.topic === "controller.reboot") {
    const b = boardState(bid);
    const phase = (ev.data || {}).phase;
    if (phase === "sent") b.reboot.phases = [];
    if (phase && !b.reboot.phases.includes(phase)) b.reboot.phases.push(phase);
    if (phase === "up") scheduleRefresh(bid, 100);
  }
  if (ev.topic === "power.cycle" && (ev.data || {}).phase === "up") scheduleRefresh(bid, 100);
  if (ev.topic === "board.found" || ev.topic === "board.lost" || ev.topic.startsWith("session.")) {
    scheduleBoards();
  }
  if (REFRESH_TOPICS.has(ev.topic)) scheduleRefresh(bid);
  changed();
}

let boardsTimer = 0;
function scheduleBoards() {
  clearTimeout(boardsTimer);
  boardsTimer = setTimeout(() => loadBoards(), 300);
}

// --- start-up ------------------------------------------------------------------------------

let socket = null;

export async function start() {
  onConnection((state) => {
    const was = S.connection;
    S.connection = state;
    if (was === "down" && state === "ok") log("ok", "daemon", "socharnessd answered again");
    if (state === "down" && was !== "down") log("warning", "daemon", "socharnessd did not answer");
    changed();
  });
  const saved = restoreSelection();
  socket = new EventSocket(handleEvent, (up) => {
    const was = S.eventsUp;
    S.eventsUp = up;
    if (up && !was) {
      // Events were missed while the socket was down: read the state again.
      loadBoards();
      for (const [bid, row] of Object.entries(S.boards)) if (row.open) scheduleRefresh(bid, 50);
    }
    changed();
  });
  socket.connect();
  const h = await timed("health", () => call("health"));
  if (!h.error) {
    S.version = h.data.data.version || "";
    S.daemon = h.data.data;             // {ok, version, pid, service}
  }
  const p = await timed("packs", () => call("packs"));
  if (!p.error) S.packs = p.data.data.packs || {};
  const r = await loadBoards();
  if (saved && S.boards[saved]) select(saved);
  else if (S.order.length) select(S.order[0]);
  if (!r.error && !S.order.length) probe();
  // Holders change under us (other users, the CLI): re-read the list now and then.
  setInterval(() => { if (document.visibilityState === "visible") loadBoards(); }, 15000);
  // A job known to hold a board is also asked after directly: its end frees the board even
  // when the event socket missed job.done (reconnecting, or events dropped).
  setInterval(() => {
    let unknown = false;
    for (const [bid, b] of Object.entries(S.board)) {
      if (!b.job) continue;
      if (b.job.id === "?") unknown = true;
      else learnJob(bid, b.job.id);
    }
    if (unknown) loadBoards();
  }, 2000);
  changed();
}
