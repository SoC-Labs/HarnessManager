// Application state, the event router, and the reads the UI makes.
//
// One mutable state object; every change calls changed(), which re-renders the app on
// the next animation frame. The app is small, so a whole-tree Preact diff is cheaper
// than bookkeeping, and nothing can show a stale copy.

import {
  call, EventSocket, heldByJob, jobEvent, onConnection, quietOf, toApiError, unwrapDebug, unwrapInfo,
} from "./api.js";
import { clock, secs, setCapabilityTitles } from "./format.js";
import { parseHash, resolveRoute, routeHash, SUBS, TAB_KEYS } from "./route.js";
import { onBackgroundState, refreshState, setViewing, VIEWER_ID, viewing } from "./viewer.js";

export const UI_NOTE = "harness-manager-ui";
const LOG_MAX = 2000;

export const S = {
  connection: "unknown",     // unknown | ok | down | auth
  eventsUp: false,
  version: "",
  packs: {},
  boards: {},                // board_id -> {board_id, open, holder, candidate, source, configured?}
  order: [],                 // board ids in rail order
  scan: { running: false, line: "", level: "", offer: [] },   // offer: boards.toml boards (SIDEBAR-UX)
  selected: null,
  sections: {},              // board_id -> tab key (route.js TABS; old keys are migrated)
  subs: {},                  // board_id -> {tab: sub-page} (Board's page, Build's step)
  // UI v2 shell: the Activity drawer ({scope: "board"|"all", level: "all"|"err"}), the toast
  // ({text, icon, level, id}), the part of a tab a link asked to show ({bid, part, at}), and
  // what the drawer last showed (the last-error chip and the rail's badge count after it).
  ui: { drawer: null, toast: null, reveal: null, seenLog: 0 },
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
      // The deploy as its events tell it; the mini bars (format.js deployBar) and the
      // Workbench's download bar read it. startedAt/doneAt/phaseAt: the events' own times
      // (epoch s); rate (bytes/s) and left (s) for the byte phases, from those times.
      deploy: { state: "idle", overlay: "", phase: "", bytes: 0, total: 0, phases: [],
        events: [], verified: false, rm_id: "", seconds: 0, transport: "", reason: "",
        stage: "", keep: false, card: null, startedAt: 0, doneAt: 0, phaseAt: 0, phaseBytes: 0,
        rate: 0, left: null },
      // Keep on the card: GET /boards/{bid}/card (read only when the harness reports "usd"),
      // and the Program section's tick box (unticked by default, cleared after each deploy).
      card: null, cardError: null, cardLoading: false, keepOnCard: false, cardFollow: 0,
      cardText: "",
      debug: null, idcode: "",
      pending: undefined, pendingSeen: false,
      consoles: null, consolesError: null, consolesLine: "", consoleSelected: null,
      reboot: { phases: [] },
      backup: null,
      arms: {},
      job: null,             // {id, kind, at}: a job harness-manager-daemon is running on this board
      session: null,         // GET /boards/{bid}/session: {adapters, reset_targets, job}
      deferred: false,       // a read was refused by the job; read again when it ends
      // QUIET-POLL: what background contact with this board does now (the daemon's gate:
      // {allowed, kind, text, holder, retry_in_s, policy}), and the last read it held back.
      background: null, quiet: null,
      // FIX-PACK-1: the quiet answer of the last held-back telemetry / card read (null once
      // one was answered): the Board tile and the Telemetry card show it instead of spinning.
      telemetryQuiet: null, cardQuiet: null,
    };
  }
  return S.board[bid];
}

// --- UI v2 routes (route.js): the tab and sub-page of each board -----------------------------

// A board behind a hub (its Checks tab, its lease badge): for an open board what GET /lease
// said once read (a read after it closed says nothing), else what boards.toml, the lease the
// service last knew, or a hub link say.
export function hubBoard(bid) {
  const row = S.boards[bid] || {};
  const w = S.board[bid] && S.board[bid].week;
  if (row.open && w && w.hubLoaded) return !!w.hub;
  const conf = row.configured || {};
  const links = (row.candidate && row.candidate.links) || [];
  return !!(conf.hub || conf.via === "hub" || row.lease_known
    || links.some((l) => l.kind === "hub" || l.via === "hub"));
}

// The tab the board shows: the one it was left on, but never Checks on a board with no hub.
export function sectionOf(bid) {
  const tab = TAB_KEYS.includes(S.sections[bid]) ? S.sections[bid] : "overview";
  return tab === "checks" && !hubBoard(bid) ? "overview" : tab;
}

// The sub-page a tab shows on this board ("" for a tab without any).
export function subOf(bid, tab = sectionOf(bid)) {
  const subs = SUBS[tab];
  if (!subs) return "";
  const want = (S.subs[bid] || {})[tab];
  return subs.includes(want) ? want : subs[0];
}

function persistRoutes() {
  try {
    window.sessionStorage.setItem("harness_manager.sections", JSON.stringify(S.sections));
    window.sessionStorage.setItem("harness_manager.subs", JSON.stringify(S.subs));
  } catch (e) { /* not remembered */ }
}

// The address bar follows the selected board's tab (history.replaceState: no history entry),
// so a reload, or the link copied from it, lands there.
export function writeRoute() {
  const bid = S.selected;
  if (!bid || typeof window === "undefined" || !window.history) return;
  const tab = sectionOf(bid);
  // the sub-page only when one was chosen: Build has no steps to land on until its lane's
  // stepper, and a bare tab opens on its first page anyway
  const chosen = (S.subs[bid] || {})[tab];
  const hash = routeHash(bid, { tab, sub: SUBS[tab] && SUBS[tab].includes(chosen) ? chosen : "" });
  if (window.location.hash === hash) return;
  try {
    window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search + hash);
  } catch (e) { /* a sandboxed page: the route stays in memory */ }
}

// Go to a board's tab: `route` is a tab key ("board"), a route ("board/versions",
// "workbench?console=uart0", "overview?activity=err") or an old 0.1.0 key ("sd", "program":
// route.js OLD_KEYS). Selects the board when it is not the selected one. The extension point
// every lane uses to send the user somewhere (UI v2 Phase 2: no lane edits app.js).
export function navigate(bid, route) {
  if (!bid) return;
  const r = resolveRoute(route);
  S.sections[bid] = r.tab;
  if (r.sub) S.subs[bid] = { ...(S.subs[bid] || {}), [r.tab]: r.sub };
  if (r.console) boardState(bid).consoleSelected = r.console;
  S.ui.reveal = r.part ? { bid, part: r.part, at: Date.now() } : null;
  persistRoutes();
  if (S.selected !== bid) select(bid);
  if (r.activity) openActivity(bid, r.activity === "err" ? "err" : "all");
  writeRoute();
  changed();
}

// 0.1.0's name for it, kept: the old keys map through route.js (a lane replaces its calls).
export function setSection(bid, section) { navigate(bid, section); }

export function select(bid) {
  S.selected = bid;
  try { window.sessionStorage.setItem("harness_manager.selected", bid || ""); } catch (e) { /* ok */ }
  writeRoute();
  changed();
  const row = S.boards[bid];
  syncViewing();
  if (row && row.open) openedBoard(bid);
}

// --- the Activity drawer, the header's last-error chip, toasts ------------------------------

// Open the Activity drawer: `bid` scopes it to that board (null: every board), `level` "err"
// shows only errors and warnings.
export function openActivity(bid = S.selected, level = "all") {
  S.ui.drawer = { scope: bid ? "board" : "all", level: level === "err" ? "err" : "all" };
  S.ui.seenLog = S.logSeq;                 // what is on screen now is seen
  changed();
}

export function closeActivity() {
  S.ui.drawer = null;
  S.ui.seenLog = S.logSeq;
  changed();
}

// The newest error or warning for a board that the drawer has not shown yet (the header's
// chip), or null.
export function lastProblem(bid) {
  for (let i = S.log.length - 1; i >= 0; i -= 1) {
    const e = S.log[i];
    if (e.id <= S.ui.seenLog) return null;
    if (e.board === bid && (e.level === "error" || e.level === "warning")) return e;
  }
  return null;
}

// Errors on any board the drawer has not shown yet (the rail's Activity badge).
export function unseenErrors() {
  let n = 0;
  for (let i = S.log.length - 1; i >= 0 && S.log[i].id > S.ui.seenLog; i -= 1) {
    if (S.log[i].level === "error") n += 1;
  }
  return n;
}

// A short note at the bottom of the page ("Copied", "Lease yours for 60 min"); it goes by
// itself. `level` "err" for a failure worth a glance (the Activity row has the detail).
export function toast(text, { icon = "circle-check", level = "", ms = 3200 } = {}) {
  const id = (S.ui.toast ? S.ui.toast.id : 0) + 1;
  S.ui.toast = { text: String(text || ""), icon, level, id };
  changed();
  setTimeout(() => {
    if (S.ui.toast && S.ui.toast.id === id) { S.ui.toast = null; changed(); }
  }, ms);
}

// --- QUIET-POLL: background reads ------------------------------------------------------------

// This page views the selected board while it is open here (viewer.js does the rest).
export function syncViewing() {
  const bid = S.selected;
  setViewing(bid && S.boards[bid] && S.boards[bid].open ? bid : null);
}

onBackgroundState((bid, state) => {
  if (!S.board[bid]) return;
  S.board[bid].background = state;
  changed();
});

// The options for a read nobody clicked: marked background, and "I am looking at it" when
// this page views that board (so its first read never waits for the viewer PUT).
export function bgOpts(bid) {
  return { background: true, viewer: viewing() === bid ? VIEWER_ID : "" };
}

// A background read the daemon held back: keep what the page last read, note why, and say
// so quietly (never an error). True when it was held back.
export function heldBack(bid, r) {
  const q = quietOf(r);
  const b = boardState(bid);
  if (!q) {
    if (!r.error && b.quiet) b.quiet = null;      // the board answered: nothing is held back
    return false;
  }
  b.quiet = { ...q, at: Date.now() };
  if (r.data.data.background) b.background = r.data.data.background;
  changed();
  return true;
}

// FIX-PACK-1: a background read held back BEFORE the card had anything to show (background
// reads are off, the lease is someone else's, the board is busy): the card says why, calmly,
// with a Read now, never a spinner that waits for a read that will not come.
export function quietWords(q) {
  if (!q) return "";
  if (q.kind === "off") return "Background reads are off";
  if (q.kind === "lease") return `Background reads paused: the hub lease is held by ${q.holder || "someone else"}`;
  if (q.kind === "lease_unknown") return "Background reads paused: the hub lease could not be read";
  if (q.kind === "busy") return "Background reads paused: the board is busy (another client)";
  if (q.kind === "no_viewer") return "Not read yet: this page is not viewing the board";
  return q.text || "Background reads are paused";
}

// The tabs this tab of the browser was on (sessionStorage), 0.1.0's keys migrated ("sd" ->
// board/versions, "program" -> workbench: route.js OLD_KEYS), then the route in the address
// bar, which wins. The board to select: the route's, else the one selected last.
export function restoreSelection() {
  let saved = null;
  try {
    const sections = JSON.parse(window.sessionStorage.getItem("harness_manager.sections") || "{}");
    const subs = JSON.parse(window.sessionStorage.getItem("harness_manager.subs") || "{}");
    if (subs && typeof subs === "object") S.subs = subs;
    if (sections && typeof sections === "object") {
      for (const [bid, key] of Object.entries(sections)) {
        const r = resolveRoute(key);           // a 0.1.0 key becomes its tab (+ sub-page)
        S.sections[bid] = r.tab;
        if (r.sub && !(S.subs[bid] || {})[r.tab]) S.subs[bid] = { ...(S.subs[bid] || {}), [r.tab]: r.sub };
      }
    }
    saved = window.sessionStorage.getItem("harness_manager.selected") || null;
  } catch (e) { /* nothing remembered */ }
  const here = parseHash(typeof window !== "undefined" ? window.location.hash : "");
  if (here) {
    applyRoute(here.bid, here.route);
    saved = here.bid;
  }
  persistRoutes();
  return saved;
}

// A route from the address bar (at start, or pasted later): its tab, sub-page and console,
// and the drawer when it asks for it. Selecting the board is the caller's.
function applyRoute(bid, r) {
  S.sections[bid] = r.tab;
  if (r.sub) S.subs[bid] = { ...(S.subs[bid] || {}), [r.tab]: r.sub };
  if (r.console) boardState(bid).consoleSelected = r.console;
  if (r.activity) S.ui.drawer = { scope: "board", level: r.activity === "err" ? "err" : "all" };
}

function onHashChange() {
  const here = parseHash(window.location.hash);
  if (!here) return;
  applyRoute(here.bid, here.route);
  persistRoutes();
  if (S.boards[here.bid] && S.selected !== here.bid) select(here.bid);
  else changed();
}

// --- jobs: while one runs, harness-manager-daemon refuses every other request on that board ------------

const JOB_LABELS = {
  deploy: "deploy", restore: "restore", debug_up: "debug session start", reboot: "board reboot",
  sd_backup: "SD backup", sd_install: "SD install", sd_restore: "SD restore", lease: "hub lease",
  lease_request: "lease request", lease_force: "force release",
  power_cycle: "power cycle", update_check: "update check", update_harness: "harness update",
  update_rollback: "harness rollback", update_app: "app update",
  update_app_rollback: "app rollback",
  // UPDATE-UI: the app's background stage, and the harness versions card's jobs
  update_stage: "app download", harness_refresh: "harness catalogue refresh",
  harness_install: "harness install", harness_rollback: "harness rollback",
  harness_fetch: "harness download",
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
  // The rail's marker comes from GET /boards rows: it ends with the job, not at the next poll.
  if (S.boards[bid] && S.boards[bid].job === id) S.boards[bid] = { ...S.boards[bid], job: null };
  const b = boardState(bid);
  if (!b.job || b.job.id !== id) return;
  const kind = b.job.kind;
  b.job = null;
  b.deferred = false;
  refreshInfo(bid, { background: true });   // the job may be another client's: nobody clicked
  if (kind === "deploy" || kind === "restore") {
    loadOverlays(bid);
    if (b.selectedOverlay) runPreflight(bid, b.selectedOverlay);
    loadCard(bid, { background: true });
  }
  if (kind === "debug_up") loadDebug(bid);
  if (kind === "reboot" || kind.startsWith("sd_")) loadPending(bid);
  for (const fn of jobEndHooks) {
    try { fn(bid, kind); } catch (e) { /* a hook never breaks the end of a job */ }
  }
  changed();
}

// A read refused because a job holds the board is not an error: wait for the job.
// harness-manager-daemon names it in the holder: "harness-manager-daemon <kind> job <id>".
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

// `job`: the job the row is about (a job.* event's, or the job the board ran when a domain
// event came), so a failure is one row (FIX-PACK-4, foldJobFailure).
export function log(level, source, text, board = "", at = null, job = "") {
  S.logSeq += 1;
  S.log.push({ id: S.logSeq, at: at || Date.now() / 1000, level, source, text, board, job: job || "" });
  if (S.log.length > LOG_MAX) S.log.splice(0, S.log.length - LOG_MAX);
  changed();
}

// FIX-PACK-4: one failed job, one Activity row. It was three: the action's own row (the
// command, rc, NAME: message, hint), the daemon's job.failed, and the domain's failure event
// (update.failed, deploy.failed). When this page ran the job, its action row is the one row:
// the daemon's failure rows for that job are dropped, now and when they arrive later. For a
// job another client ran, the domain's row (it has the reason) stands and job.failed folds
// into it; with no domain row, job.failed is the one row, in words.
const foldedJobs = new Set();

function isFailureRow(e) { return e.level === "error" && /\.failed$/.test(e.source); }

export function foldJobFailure(bid, jobId) {
  if (!jobId) return;
  foldedJobs.add(jobId);
  if (foldedJobs.size > 500) foldedJobs.delete(foldedJobs.values().next().value);
  let dropped = false;
  for (let i = S.log.length - 1; i >= 0; i -= 1) {
    const e = S.log[i];
    if (e.job === jobId && e.board === bid && isFailureRow(e)) { S.log.splice(i, 1); dropped = true; }
  }
  if (dropped) changed();
}

function jobFailedText(d, kind) {
  const e = d.error || {};
  return `${kind ? `${jobLabel(kind)} ` : ""}job failed: ${e.name ? `${e.name}: ` : ""}${e.message || "no reason given"}`
    + `${e.hint ? `  hint: ${e.hint}` : ""}`;
}

// --- the service's own environment (FIX-PACK-2) -------------------------------------------

// GET /daemon/env: {env, overrides, warning}. The service keeps the environment of the shell
// that started it, so a tool variable there can hide the user's setting; the app says so.
export async function loadServiceEnv() {
  const r = await timed("daemon env", () => call("daemonEnv"));
  if (!r.error) {
    const d = r.data.data || {};
    S.serviceEnv = { env: d.env || {}, overrides: d.overrides || [], warning: d.warning || "" };
    changed();
  }
  return r;
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

// This page opened or closed the board after a list was asked for: that list is older
// than what the page knows, and its "open" must not undo the click. A list asked before
// Open and answered after it put a just-opened board back to its preview (the Shell fact
// never came: FLAKE 2026-09-24); only the next list, if any, brought it back.
export function openedOrClosedHere(bid, open) {
  S.boards[bid] = { ...S.boards[bid], open, openChangedAt: Date.now() };
  syncViewing();
}

function newerHere(bid, asked) {
  const at = S.boards[bid] && S.boards[bid].openChangedAt;
  return asked !== null && !!at && at >= asked;
}

function mergeBoards(rows, asked = null) {
  for (const row of rows) {
    const prev = S.boards[row.board_id] || {};
    const next = { ...prev, ...row };
    if (newerHere(row.board_id, asked)) next.open = prev.open;
    S.boards[row.board_id] = next;
    if (!S.order.includes(row.board_id)) S.order.push(row.board_id);
  }
}

export async function loadBoards() {
  const asked = Date.now();
  const r = await timed("boards", () => call("boards"));
  if (r.error) return r;
  const rows = r.data.data.boards || [];
  const seen = new Set(rows.map((b) => b.board_id));
  for (const bid of Object.keys(S.boards)) {
    // A board this page knows but the daemon no longer lists stays, marked not open.
    if (!seen.has(bid) && !newerHere(bid, asked)) S.boards[bid] = { ...S.boards[bid], open: false };
  }
  mergeBoards(rows, asked);
  for (const row of rows) {
    if (row.job) setJob(row.board_id, row.job, null);
    else if (S.board[row.board_id] && S.board[row.board_id].job && row.open
             && S.board[row.board_id].job.at < asked) {
      // The list says no job: one we missed the end of (the event socket was down).
      // Only for a job known before this list was asked for: one that started while the
      // request was in flight is newer than the list, and ending it here would free a
      // board its job still holds, for good (endedJobs never lets it back).
      jobEnded(row.board_id, S.board[row.board_id].job.id);
    }
    if (S.boards[row.board_id].open) openedBoard(row.board_id, { quiet: true });
  }
  syncViewing();
  changed();
  return r;
}

// Probes run one after another: an address added while a scan runs waits its turn
// instead of being dropped.
let probeChain = Promise.resolve();
let userProbes = 0;          // probes the user asked for (Scan, Add by address)

export function probe(hosts = null, via = "", { auto = false } = {}) {
  if (!auto) userProbes += 1;
  const run = () => probeNow(hosts, via, auto);
  probeChain = probeChain.then(run, run);
  return probeChain;
}

async function probeNow(hosts, via = "", auto = false) {
  // The page's own first scan (no boards listed) gives way to a probe the user asked for
  // meanwhile: an address added while the first list loaded ran first, and the scan
  // chained after it replaced the add's answer on the status line (FLAKE 2026-09-24,
  // CI run 35996099505).
  if (auto && (userProbes || S.order.length)) return;
  S.scan = { running: true, line: hosts ? `adding ${hosts.join(", ")}${via ? ` ${via}` : ""}...` : "scanning...", level: "" };
  changed();
  // With explicit hosts the pack pings only those and unicasts identify to them, so a board
  // in stage0 rescue (identify, no 6900) is still found: keep scan_network on. `via`
  // ("ssh:HOST") reaches them through that hub's tunnel.
  const body = hosts ? { hosts, scan_usb: false, ...(via ? { via } : {}) } : {};
  const what = hosts ? `probe ${hosts.join(" ")}${via ? ` --via ${via}` : ""}` : "probe";
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
  // SIDEBAR-UX: a scan also offers the boards boards.toml configures (GET /boards lists
  // them, not contacted): they may be behind a hub, where no scan of this network finds
  // them; Open reaches each through its own via/hub.
  const found = new Set(cands.map((c) => c.board_id));
  const offer = hosts ? [] : S.order.filter((bid) => S.boards[bid] && S.boards[bid].configured
    && !S.boards[bid].open && !found.has(bid));
  const more = offer.length ? `; ${offer.length} more in boards.toml (not contacted)` : "";
  S.scan = {
    running: false,
    line: n ? `${r.line}: ${n} board${n === 1 ? "" : "s"}${more}`
      : offer.length ? `${r.line}: no boards answered on this network${more}`
        : `${r.line}: no boards answered. Check the Ethernet link, or add one by address.`,
    level: n || offer.length ? "" : "warn",
    offer,
  };
  log(n || offer.length ? "info" : "warning", "probe", S.scan.line);
  if (!S.selected && S.order.length) select(firstBoard() || S.order[0]);
  if (hosts && cands.length) select(cands[0].board_id);
  changed();
}

// SIDEBAR-UX: the board the rail shows first (favourites and the user's order), selected
// when nothing is; sidebar.js sets it.
let firstBoard = () => S.order[0];
let firstReady = Promise.resolve();
export function setFirstBoard(fn, ready = null) {
  firstBoard = fn;
  if (ready) firstReady = ready;
}

// After a board is open in the daemon: read everything the workspace shows.
export function openedBoard(bid, { quiet = false } = {}) {
  const b = boardState(bid);
  if (b.readOnOpen) {
    // QUIET-POLL: its first read came while no page viewed it and was held back; this page
    // views it now, so read it (only the board's info: the rest follows from it).
    if (b.quiet && b.quiet.kind === "no_viewer" && viewing() === bid && !b.infoLoading) {
      refreshInfo(bid, { background: true });
      for (const fn of openHooks) {
        try { fn(bid); } catch (e) { /* a hook never breaks the open */ }
      }
    }
    return;
  }
  b.readOnOpen = true;
  refreshInfo(bid, { background: true });   // nobody clicked: the daemon's gate decides
  loadSession(bid);
  loadPending(bid);
  loadDebug(bid);
  for (const fn of openHooks) {
    try { fn(bid); } catch (e) { /* a hook never breaks the open */ }
  }
  if (!quiet) changed();
}

// `background`: a read nobody clicked (QUIET-POLL): the daemon may hold it back.
export async function refreshInfo(bid, { background = false } = {}) {
  const b = boardState(bid);
  // A read nobody clicked never supersedes one in flight: the gate may hold it back, and the
  // newer-wins rule below would then drop the answer the user asked for.
  if (background && b.infoLoading) return;
  b.infoGen = (b.infoGen || 0) + 1;
  const gen = b.infoGen;
  b.infoLoading = true;
  changed();
  const r = await timed("info", () => call("info", { bid }, undefined, null,
    background ? bgOpts(bid) : {}));
  if (gen !== b.infoGen) return;          // a newer read is on its way: it wins
  b.infoLoading = false;
  if (heldBack(bid, r)) {
    // Asked before this page said it views the board (the list loaded before the selection):
    // now that it does, read again, once.
    if (b.quiet.kind === "no_viewer" && viewing() === bid && Date.now() - (b.quietRetryAt || 0) > 5000) {
      b.quietRetryAt = Date.now();
      scheduleRefresh(bid, 300);
    } else if (b.info) {
      // FIX-PACK-1: the page shows the board (the open's own answer) but the reads that follow
      // a good read (telemetry, the card line) were not made either: the Board tile says why,
      // with Read now, instead of "reading..." while nothing reads.
      if (!b.telemetry && !b.telemetryLoading) b.telemetryQuiet = b.quiet;
      if (hasCardStore(b) && !b.card && !b.cardLoading) b.cardQuiet = b.quiet;
      changed();
    }
    return;
  }
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
    b.quiet = null;
    if (!background) refreshState(bid);      // a click: the gate's state may have moved too
    if (!b.telemetry && !b.telemetryLoading) loadTelemetry(bid, { background: true });
    // LINUX-SLOTS: the Board tile's Card line reads the same card (a harness with "usd").
    if (hasCardStore(b) && !b.card && !b.cardLoading) loadCard(bid, { background: true });
  }
  changed();
}

// FIX-PACK-4: the header's "Read the board again": the board's info, and with it what the
// workspace shows beside it that a read of the info alone never re-reads: the SD journal (the
// interrupted-install banner) and, on a harness with a user microSD, the Card line. A click,
// so none of it is a background read.
export async function rereadBoard(bid) {
  loadPending(bid);
  await refreshInfo(bid);
  const b = boardState(bid);
  if (hasCardStore(b) && !b.cardLoading) loadCard(bid);
}

const refreshTimers = {};
export function scheduleRefresh(bid, ms = 250) {
  clearTimeout(refreshTimers[bid]);
  refreshTimers[bid] = setTimeout(() => {
    if (!S.boards[bid] || !S.boards[bid].open) return;
    if (S.board[bid] && S.board[bid].job) S.board[bid].deferred = true;   // after the job
    else refreshInfo(bid, { background: true });
  }, ms);
}

export async function loadTelemetry(bid, { background = false } = {}) {
  const b = boardState(bid);
  if (b.telemetryLoading) return;
  b.telemetryLoading = true;
  changed();
  const r = await timed("telemetry", () => call("telemetry", { bid }, undefined, null,
    background ? bgOpts(bid) : {}));
  b.telemetryLoading = false;
  b.telemetryAt = Date.now();
  if (heldBack(bid, r)) { b.telemetryQuiet = b.quiet; changed(); return; }
  b.telemetryQuiet = null;
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

// The harness keeps designs on its user microSD only when it reports "usd" (net-protocol
// v0.13). Without it the card is never read and the "Keep on the card" box never shows.
export function hasCardStore(b) {
  const feats = b && b.info && b.info.identity && b.info.identity.features;
  return Array.isArray(feats) && feats.includes("usd");
}

export async function loadCard(bid, { background = false } = {}) {
  const b = boardState(bid);
  if (!hasCardStore(b)) {
    b.card = null;
    b.cardError = null;
    b.keepOnCard = false;
    changed();
    return;
  }
  if (b.cardLoading) return;
  b.cardLoading = true;
  changed();
  const r = await timed("card", () => call("card", { bid }, undefined, null,
    background ? bgOpts(bid) : {}));
  b.cardLoading = false;
  if (heldBack(bid, r)) { b.cardQuiet = b.quiet; changed(); return; }
  b.cardQuiet = null;
  if (r.error && deferIfHeld(bid, r.error)) return;
  b.cardError = r.error;
  b.card = r.error ? null : (r.data.data.card || null);
  // SLOT-TIMING: the service's one line ("writing slot B: 12.3 MB / 29 MB, ~6 min left")
  b.cardText = r.error ? "" : (r.data.data.line || "");
  if (!b.card || !b.card.store || !b.card.present || b.card.reason) b.keepOnCard = false;
  changed();
  // SLOT-TIMING: while the card job writes or reads back, the Card line follows it.
  if (cardJobBusy(b.card) && !b.cardFollow) {
    b.cardFollow = setTimeout(() => { b.cardFollow = 0; loadCard(bid, { background: true }); },
      CARD_FOLLOW_MS);
  }
}

// How often the Card line re-reads a board whose card job is running (each read is one
// `slot status`, ~7 sectors off the same card: not faster).
export const CARD_FOLLOW_MS = 5000;

// SLOT-TIMING: the OS-slot card job is writing or reading back (nothing may reset the board).
export function cardJobBusy(card) {
  const job = card && card.os_slots && card.os_slots.job;
  return !!job && (job.state === "writing" || job.state === "verifying");
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
    // recovery before anything else: Board > Versions holds the Configuration SD (0.1.0's "sd")
    S.sections[bid] = "board";
    S.subs[bid] = { ...(S.subs[bid] || {}), board: "versions" };
    persistRoutes();
    if (S.selected === bid) writeRoute();
    log("error", "storage", "Interrupted SD install: restore it first (Board > Versions)", bid);
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
  if (ev.topic === "deploy.warning") return "warning";       // FIX-PACK-7: a forced swap
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
  const at = Number(ev.at) || Date.now() / 1000;
  if (ev.topic === "deploy.warning") {
    dep.nextWarning = d.message || "";     // FIX-PACK-7: published just before deploy.started
  } else if (ev.topic === "deploy.started") {
    Object.assign(dep, { state: "running", overlay: d.overlay || d.rm_id || "", phase: "started",
      bytes: 0, total: 0, phases: [], events: [], verified: false, reason: "", stage: "",
      rm_id: d.rm_id || "", keep: !!d.keep_on_card, card: null, startedAt: at, doneAt: 0,
      phaseAt: at, phaseBytes: 0, rate: 0, left: null, warning: dep.nextWarning || "" });
    dep.nextWarning = "";
    dep.events.push(`${clock(ev.at)}  started ${dep.overlay}`);
  } else if (ev.topic === "deploy.progress") {
    if (dep.state !== "running") {
      Object.assign(dep, { state: "running", phases: [], events: [], startedAt: at, doneAt: 0 });
    }
    const phase = d.phase || "?";
    const bytes = Number(d.bytes || 0);
    if (phase !== dep.phase) {
      Object.assign(dep, { phaseAt: at, phaseBytes: bytes, rate: 0, left: null });
    } else if (at > dep.phaseAt && bytes > dep.phaseBytes) {
      dep.rate = (bytes - dep.phaseBytes) / (at - dep.phaseAt);
      dep.left = dep.rate > 0 ? Math.max(0, (Number(d.total || 0) - bytes) / dep.rate) : null;
    }
    dep.phase = phase;
    dep.bytes = bytes;
    dep.total = Number(d.total || 0);
    if (!dep.phases.includes(dep.phase)) dep.phases.push(dep.phase);
    dep.events.push(`${clock(ev.at)}  ${dep.phase} ${dep.bytes}/${dep.total}`);
  } else if (ev.topic === "deploy.done") {
    Object.assign(dep, { state: "done", phase: "done", verified: !!d.verified,
      rm_id: d.rm_id || "", seconds: Number(d.seconds || 0), transport: d.transport || "",
      card: d.card || null, doneAt: at, rate: 0, left: null });
    if (dep.total) dep.bytes = dep.total;
    dep.events.push(`${clock(ev.at)}  done rm_id ${d.rm_id} verified=${d.verified ? "yes" : "no"}`);
  } else if (ev.topic === "deploy.failed") {
    Object.assign(dep, { state: "failed", reason: d.reason || "no reason given",
      stage: d.stage || "", doneAt: at, rate: 0, left: null });
    if (d.overlay) dep.overlay = d.overlay;
    dep.events.push(`${clock(ev.at)}  failed: ${dep.reason}`);
  }
  if (dep.events.length > 200) dep.events.splice(0, dep.events.length - 200);
}

// Other modules (week.js) follow events and board opens without store.js importing them.
const eventHooks = [];
const openHooks = [];
const jobEndHooks = [];
const reconnectHooks = [];
export function onBoardEvent(fn) { eventHooks.push(fn); }
export function onBoardOpened(fn) { openHooks.push(fn); }
export function onJobEnded(fn) { jobEndHooks.push(fn); }
// UPDATE-UI: the event socket came back (the daemon may have restarted onto a new version).
export function onEventsReconnected(fn) { reconnectHooks.push(fn); }

export function handleEvent(ev) {
  const bid = ev.board_id || "";
  if (ev.topic.startsWith("job.")) jobEvent(ev);
  for (const fn of eventHooks) {
    try { fn(ev); } catch (e) { /* a hook never breaks the router */ }
  }
  if (ev.topic === "events.dropped") {
    // The page fell behind and the daemon dropped events: read the state again.
    log("warning", "events", `the daemon dropped ${(ev.data || {}).dropped} events for this page; reading again`);
    loadBoards();
    for (const [id, row] of Object.entries(S.boards)) if (row.open) scheduleRefresh(id, 50);
    return;
  }
  if (bid && ev.topic === "job.started") setJob(bid, (ev.data || {}).job, (ev.data || {}).kind);
  // FIX-PACK-4: the job a row is about, taken before job.done/job.failed ends it
  const running = bid && S.board[bid] && S.board[bid].job;
  const jobId = ev.topic.startsWith("job.") ? (ev.data || {}).job || "" : (running && running.id) || "";
  const jobKind = running && running.id === jobId ? running.kind : "";
  if (bid && (ev.topic === "job.done" || ev.topic === "job.failed")) jobEnded(bid, (ev.data || {}).job);
  if (ev.topic !== "job.progress" && ev.topic !== "checks.progress") {
    const level = eventLevel(ev);
    const failure = level === "error" && ev.topic.endsWith(".failed");
    if (failure && jobId && foldedJobs.has(jobId)) {
      // this page's action row already says it
    } else if (ev.topic === "job.failed") {
      const told = S.log.some((e) => e.job === jobId && e.board === bid && isFailureRow(e));
      if (!told) log(level, ev.topic, jobFailedText(ev.data || {}, jobKind), bid, ev.at, jobId);
    } else {
      log(level, ev.topic, eventText(ev), bid, ev.at, jobId);
    }
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
    // UI v2 (CCR WORKBENCH-2): DEBUG-ONBOARD's additive fields, as the status read gives them
    if (d.where !== undefined) b.debug.where = d.where;
    if (Array.isArray(d.gdb_ports)) b.debug.gdb_ports = d.gdb_ports;
    if (Array.isArray(d.cores)) b.debug.cores = d.cores;
    if (d.state !== "up" && d.state !== "starting") {
      b.debug.gdb_port = 0; b.debug.telnet_port = 0; b.debug.tcl_port = 0;
      if (b.debug.gdb_ports) b.debug.gdb_ports = [];
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
    if (was === "down" && state === "ok") log("ok", "daemon", "harness-manager-daemon answered again");
    if (state === "down" && was !== "down") log("warning", "daemon", "harness-manager-daemon did not answer");
    changed();
  });
  const saved = restoreSelection();
  window.addEventListener("hashchange", onHashChange);
  socket = new EventSocket(handleEvent, (up) => {
    const was = S.eventsUp;
    S.eventsUp = up;
    if (up && !was) {
      // Events were missed while the socket was down: read the state again.
      loadBoards();
      for (const [bid, row] of Object.entries(S.boards)) if (row.open) scheduleRefresh(bid, 50);
      for (const fn of reconnectHooks) {
        try { fn(); } catch (e) { /* a hook never breaks the reconnect */ }
      }
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
  if (!p.error) {
    S.packs = p.data.data.packs || {};
    setCapabilityTitles(p.data.data.capabilities);
  }
  // FIX-PACK-2: the variables the service started with; read again when a setting changes
  // (yours may now be hidden, or not) and when the service answers again (it may be new).
  loadServiceEnv();
  onBoardEvent((ev) => { if (ev.topic === "settings.changed") loadServiceEnv(); });
  onEventsReconnected(() => loadServiceEnv());
  const r = await loadBoards();
  // SIDEBAR-UX: the user's order decides the first board; it is read at start (never
  // waited for long: a settings service that does not answer leaves the page's order).
  await Promise.race([firstReady, new Promise((ok) => { setTimeout(ok, 2000); })]);
  // A board clicked while this list loaded stays selected: the event socket's own read
  // can draw the rail first, and selecting here would switch the user's pick back.
  if (!S.selected) {
    if (saved && S.boards[saved]) select(saved);
    else if (S.order.length) select(firstBoard() || S.order[0]);
  }
  if (!r.error && !S.order.length) probe(null, "", { auto: true });
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
