// The harness-manager-daemon client: token, JSON calls, jobs, and the event socket (docs/API.md v1).
//
// Every endpoint the UI calls is listed in ENDPOINTS. A test checks each entry
// against the endpoint table in docs/API.md, so the UI cannot drift from the
// contract without failing `make check`. ADDITIVE may name routes harness-manager-daemon serves
// beyond API.md; the test checks those against the daemon's own route table instead,
// and fails once API.md lists them (then they leave ADDITIVE). It is empty today.

export const ENDPOINTS = Object.freeze({
  health: ["GET", "/health"],
  packs: ["GET", "/packs"],
  probe: ["POST", "/probe"],
  boards: ["GET", "/boards"],
  openBoard: ["POST", "/boards"],
  closeBoard: ["DELETE", "/boards/{bid}"],
  info: ["GET", "/boards/{bid}"],
  telemetry: ["GET", "/boards/{bid}/telemetry"],
  overlays: ["GET", "/boards/{bid}/overlays"],
  card: ["GET", "/boards/{bid}/card"],
  preflight: ["POST", "/boards/{bid}/preflight"],
  deploy: ["POST", "/boards/{bid}/deploy"],
  restore: ["POST", "/boards/{bid}/restore"],
  reset: ["POST", "/boards/{bid}/reset"],
  consoles: ["GET", "/boards/{bid}/consoles"],
  consoleSocket: ["WS", "/boards/{bid}/consoles/{name}"],
  consoleExport: ["POST", "/boards/{bid}/consoles/{name}/export"],
  debugStatus: ["GET", "/boards/{bid}/debug"],
  debugDetect: ["POST", "/boards/{bid}/debug/detect"],
  debugUp: ["POST", "/boards/{bid}/debug/up"],
  debugDown: ["POST", "/boards/{bid}/debug/down"],
  reboot: ["POST", "/boards/{bid}/controller/reboot"],
  sdPending: ["GET", "/boards/{bid}/storage/pending"],
  sdBackup: ["POST", "/boards/{bid}/storage/backup"],
  sdRestore: ["POST", "/boards/{bid}/storage/restore"],
  sdInstall: ["POST", "/boards/{bid}/storage/install"],
  session: ["GET", "/boards/{bid}/session"],
  clocks: ["GET", "/boards/{bid}/clocks"],
  clockSet: ["POST", "/boards/{bid}/clocks"],
  osc: ["GET", "/boards/{bid}/controller/osc"],
  // --- P3 PANEL-UI: the front panel (docs/API.md "Front panel", panel_api.py) ---
  panel: ["GET", "/boards/{bid}/panel"],
  // --- LINUX-CLAIM: the Linux harness's SSH claim (docs/API.md, claim_api.py) ---
  claim: ["POST", "/boards/{bid}/claim"],
  // --- BOARD-ID: the board's label/IP/MAC and the fix (docs/API.md "Board identity") ---
  netIdentity: ["GET", "/boards/{bid}/identity"],
  netIdentityFix: ["POST", "/boards/{bid}/identity"],
  panelFrame: ["GET", "/boards/{bid}/panel/frame"],
  identify: ["POST", "/boards/{bid}/identify"],
  // --- end P3 PANEL-UI ---
  // --- LM4 DISPLAY-UI: the Live display (docs/API.md "Live display", display_api.py). The
  // socket's ?ack=&rate= and the PNG's ?scale=&hatch= are the query (socketUrl, callBytes).
  displaySocket: ["WS", "/boards/{bid}/display/ws"],
  displayPng: ["GET", "/boards/{bid}/display.png"],
  // --- end LM4 DISPLAY-UI ---
  // Week-plan additions (docs/API.md, frozen): lanes L2 consoles, L1 hub, L4 power and update.
  ptyOpen: ["POST", "/boards/{bid}/consoles/{name}/pty"],
  ptyGet: ["GET", "/boards/{bid}/consoles/{name}/pty"],
  baudGet: ["GET", "/boards/{bid}/consoles/{name}/baud"],
  baudSet: ["POST", "/boards/{bid}/consoles/{name}/baud"],
  tunnel: ["GET", "/boards/{bid}/tunnel"],
  lease: ["GET", "/boards/{bid}/lease"],
  leaseTake: ["POST", "/boards/{bid}/lease"],
  leaseRelease: ["DELETE", "/boards/{bid}/lease"],
  // Lease requests, force release, leaving the queue (docs/LEASE_REQUESTS.md, frozen; LR-C).
  leaseRequest: ["POST", "/boards/{bid}/lease/request"],
  leaseRespond: ["POST", "/boards/{bid}/lease/respond"],
  leaseForce: ["POST", "/boards/{bid}/lease/force"],
  leaseLeave: ["DELETE", "/boards/{bid}/lease/queue"],
  leaseTakenDismiss: ["DELETE", "/boards/{bid}/lease/taken"],      // D11
  power: ["GET", "/boards/{bid}/power"],
  powerCycle: ["POST", "/boards/{bid}/power/cycle"],
  updateCheck: ["POST", "/update/check"],
  updateHarness: ["POST", "/boards/{bid}/update/harness"],
  updateRollback: ["POST", "/boards/{bid}/update/rollback"],
  updateApp: ["POST", "/update/app"],
  // T10: XDC export (docs/API.md "XDC export", xdc_api.py).
  boardXdc: ["GET", "/boards/{bid}/xdc"],
  boardXdcExport: ["POST", "/boards/{bid}/xdc/export"],
  helpTabs: ["GET", "/help/tabs"],
  daemonEnv: ["GET", "/daemon/env"],          // FIX-PACK-2: the service's own tool variables
  job: ["GET", "/jobs/{id}"],
  events: ["WS", "/events"],
  // --- KIT-UI: the Build section (docs/API.md "DUT build kits and the build guide") ---
  boardKit: ["GET", "/boards/{bid}/kit"],
  boardGuide: ["GET", "/boards/{bid}/guide"],          // ?design=&build_dir= (call's query)
  kitGet: ["GET", "/kits/{static_id}"],
  kitFetch: ["POST", "/kits/fetch"],
  kitZip: ["GET", "/kits/{static_id}/zip"],
  guideScript: ["POST", "/guide/script"],
  kitCheck: ["POST", "/kits/check"],
  kitPack: ["POST", "/kits/pack"],
  // --- end KIT-UI ---
  // --- ui2 api-build --- UI v2's readings, OS slots and card, import and build (docs/API.md
  // "Readings kept by this service", "OS slots and the card: roll back, commit, clear",
  // "Import a design, and the build's progress, floorplan and utilisation"). The history's
  // ?name=&since=&limit= is the call's query argument; guide's ?static_id=&design=&build_dir=.
  readingsHistory: ["GET", "/boards/{bid}/readings/history"],
  slots: ["GET", "/boards/{bid}/slots"],
  slotRollback: ["POST", "/boards/{bid}/slots/rollback"],
  cardCommit: ["POST", "/boards/{bid}/card/commit"],
  cardClear: ["POST", "/boards/{bid}/card/clear"],
  overlayImport: ["POST", "/overlays/import"],
  overlayUpload: ["POST", "/overlays/upload"],    // body: the zip's bytes (a raw-body call)
  designScan: ["POST", "/kits/design/scan"],
  guide: ["GET", "/guide"],
  // --- end ui2 api-build ---
  // --- XVC-UI: the Debug section's XVC card (docs/API.md "Fabric debug over XVC", xvc_api.py).
  // The query is part of the template (callBlob takes no query argument): pass byo "" (the
  // open session's mode), "true" or "false"; which "auto" (the full-design file when the
  // mint staged one, else the RM's), "rm", "static" or "full".
  xvcStatus: ["GET", "/boards/{bid}/xvc"],
  xvcOpen: ["POST", "/boards/{bid}/xvc/open"],
  xvcClose: ["POST", "/boards/{bid}/xvc/close"],
  xvcTcl: ["GET", "/boards/{bid}/xvc/tcl?byo={byo}"],
  xvcLtx: ["GET", "/boards/{bid}/xvc/ltx?which={which}"],
  // --- end XVC-UI ---
  // --- UPDATE-UI: the app's own update (OTA-U; docs/API.md "App self-update", update_api.py)
  // and a board's harness versions (H9; docs/API.md "Harness versions", harness_api.py).
  // The queries (?board_id=&channel=, ?limit=) are the call's query argument.
  appUpdate: ["GET", "/update/app"],
  appApply: ["POST", "/update/app/apply"],
  appCancel: ["POST", "/update/app/cancel"],
  updateSettings: ["GET", "/update/settings"],
  updateSettingsSet: ["PUT", "/update/settings"],
  harnessCatalog: ["GET", "/harness/catalog"],
  harnessRefresh: ["POST", "/harness/catalog/refresh"],
  harnessRelease: ["GET", "/harness/releases/{version}"],
  harnessInstall: ["POST", "/boards/{bid}/harness/install"],
  harnessPin: ["PUT", "/boards/{bid}/harness/pin"],
  harnessUnpin: ["DELETE", "/boards/{bid}/harness/pin"],
  harnessHistory: ["GET", "/boards/{bid}/harness/history"],
  harnessRollback: ["POST", "/boards/{bid}/harness/rollback"],
  // --- end UPDATE-UI ---
  // --- SET-API: the settings (docs/API.md "Settings", settings_api.py). The Settings dialog
  // (SET-UI, js/settings/*) calls them. The query (?section=&key=&all=) is the call's query
  // argument; {key} is a setting's key, URL-encoded. A secret is written, never read back.
  settings: ["GET", "/settings"],
  settingsSchema: ["GET", "/settings/schema"],
  settingsSet: ["PUT", "/settings"],
  settingsUnset: ["DELETE", "/settings/{key}"],
  settingsSecretSet: ["PUT", "/settings/secrets/{key}"],
  settingsSecretUnset: ["DELETE", "/settings/secrets/{key}"],
  settingsTest: ["POST", "/settings/test"],
  // --- end SET-API ---
  // --- SET-UI: the Settings dialog's Hubs section (docs/API.md "Hubs in the Settings dialog",
  // hubs_api.py). Test connection and Tools Detect are settingsTest above ({section, name}).
  hubs: ["GET", "/hubs"],
  hubPut: ["PUT", "/hubs/{name}"],
  hubRemove: ["DELETE", "/hubs/{name}"],
  hubAddBoard: ["POST", "/hubs/{name}/boards"],
  hubAdopt: ["POST", "/hubs/adopt"],
  // --- end SET-UI ---
  // --- QUIET-POLL: which board this page shows (docs/API.md "Background reads", quiet_api.py)
  viewerPut: ["PUT", "/boards/{bid}/viewers/{vid}"],
  viewerDelete: ["DELETE", "/boards/{bid}/viewers/{vid}"],
  background: ["GET", "/boards/{bid}/background"],
  // --- HIL-GUI: the Checks section (docs/API.md "HIL checks", hil_api.py). The report's
  // ?iteration=&name= is the call's query argument.
  checks: ["GET", "/boards/{bid}/checks"],
  checksStart: ["POST", "/boards/{bid}/checks"],
  checksStop: ["DELETE", "/boards/{bid}/checks"],
  checksReport: ["GET", "/boards/{bid}/checks/{run}/report"],
  // --- end HIL-GUI
  // --- ui2 api-hub ---
  // docs/API.md "UI v2: hub leases, the Debug USB route, consoles and clashes". The lease
  // routes above also take a board that is not open (G3), and leaseRequest takes want_s
  // (G11); ?refresh=1 on hubLeases is the call's query argument.
  hubLeases: ["GET", "/hubs/{name}/leases"],                  // G3: every target's lease, one read
  identityClashes: ["GET", "/identity/clashes"],               // G10: across every board seen
  // --- end ui2 api-hub ---
});

export const ADDITIVE = Object.freeze([]);

// Topics the UI follows (docs/CONTRACTS.md). console.line is left out on purpose:
// console bytes arrive on each console's own socket, so a line is never shown twice.
export const EVENT_TOPICS = [
  "board.*", "session.*", "deploy.*", "console.state", "console.pty", "debug.*",
  "controller.*", "storage.*", "update.*", "power.*", "lease.*", "tunnel.*", "job.*", "events.*",
  "panel.*",                           // P3 PANEL-UI: panel.state, panel.tap, panel.locate
  "display.*",                         // LM4 DISPLAY-UI: display.state (a refused Live display asks again)
  "xvc.*",                             // XVC-UI: xvc.state, the Debug section's XVC card
  "harness.*",                         // UPDATE-UI: harness.catalog|installing|installed|pinned
  "settings.*",                        // SET-UI: settings.changed (the dialog and the restart banner)
  "checks.*",                          // HIL-GUI: checks.state, checks.progress (the Checks section)
  "design.*",                          // FIX-PACK-6: design.check, the cold-boot DAP cross-check
];

const TOKEN_KEY = "harness_manager.token";
let token = "";

// --- the token: #token=... -> sessionStorage, then gone from the address bar -----------

export function initToken() {
  const hash = window.location.hash.replace(/^#/, "");
  const params = new URLSearchParams(hash);
  const given = params.get("token");
  if (given) {
    token = given;
    try { window.sessionStorage.setItem(TOKEN_KEY, given); } catch (e) { /* memory only */ }
    params.delete("token");
    const rest = params.toString();
    const url = window.location.pathname + window.location.search + (rest ? `#${rest}` : "");
    window.history.replaceState(null, "", url);
  } else {
    try { token = window.sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { token = ""; }
  }
  return token !== "";
}

export function hasToken() { return token !== ""; }

// --- base URL: api/v1 relative to the page, unless the host says otherwise --------------

function apiBase() {
  const meta = document.querySelector('meta[name="harness-manager-api-base"]');
  const given = meta && meta.getAttribute("content");
  const base = new URL(given || "api/v1/", document.baseURI);
  if (!base.pathname.endsWith("/")) base.pathname += "/";
  return base;
}

export function fillPath(template, params = {}) {
  return template.replace(/\{(\w+)\}/g, (_, key) => {
    if (params[key] === undefined || params[key] === null) {
      throw new Error(`missing path parameter ${key} for ${template}`);
    }
    return encodeURIComponent(String(params[key]));
  });
}

export function endpointUrl(name, params = {}) {
  const [, template] = ENDPOINTS[name];
  return new URL(fillPath(template, params).replace(/^\//, ""), apiBase());
}

export function socketUrl(name, params = {}, query = {}) {
  const url = endpointUrl(name, params);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("token", token);
  for (const [k, v] of Object.entries(query)) url.searchParams.set(k, v);
  return url.toString();
}

// --- errors -------------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(error, status = 0, transport = false) {
    const e = error || {};
    super(e.message || "the request failed");
    this.code = e.code ?? null;
    this.errName = e.name || (transport ? "NO_ANSWER" : `HTTP_${status}`);
    this.hint = e.hint || "";
    this.holder = e.holder || "";
    this.capability = e.capability || "";
    this.reason = e.reason || "";
    this.data = e.data;
    this.status = status;
    this.transport = transport;
  }
}

export function toApiError(err) {
  if (err instanceof ApiError) return err;
  return new ApiError({ name: "FAILED", message: String(err && err.message || err) });
}

// --- connection state ----------------------------------------------------------------------

const connectionListeners = new Set();
let connection = "unknown";      // unknown | ok | down | auth

export function onConnection(fn) {
  connectionListeners.add(fn);
  return () => connectionListeners.delete(fn);
}

function setConnection(state) {
  if (state === connection) return;
  connection = state;
  for (const fn of connectionListeners) fn(state);
}

// --- calls ---------------------------------------------------------------------------------

// QUIET-POLL: a read nobody clicked carries X-HM-Background (the daemon then answers it
// with `quiet` instead of touching the board when its background gate says no); the page's
// own viewed board also carries X-HM-Viewer.
async function send(name, params, body, accept, query = null, opts = {}) {
  const [method] = ENDPOINTS[name];
  const url = endpointUrl(name, params);
  // KIT-UI: a query string (GET /boards/{bid}/guide?design=&build_dir=); empty values are left out.
  for (const [k, v] of Object.entries(query || {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  const headers = { Accept: accept };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.background) headers["X-HM-Background"] = "1";
  if (opts.viewer) headers["X-HM-Viewer"] = opts.viewer;
  const init = { method, headers, cache: "no-store" };
  if (opts.keepalive) init.keepalive = true;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  try {
    return await fetch(url, init);
  } catch (e) {
    setConnection("down");
    throw new ApiError({
      name: "NO_ANSWER",
      message: "harness-manager-daemon did not answer",
      hint: "check it is running: harness-manager daemon status",
    }, 0, true);
  }
}

// A failed answer as an ApiError: 401 marks the session expired; anything else carries the
// daemon's error envelope (or says there was none).
function failure(res, data) {
  if (res.status === 401) {
    // harness-manager-daemon answers a missing or stale token with 401 and REFUSED (15).
    setConnection("auth");
    return new ApiError((data && data.error) || {
      name: "REFUSED", code: 15, message: "session expired",
      hint: "run harness-manager ui again",
    }, 401);
  }
  setConnection("ok");
  return new ApiError((data && data.error) || {
    name: `HTTP_${res.status}`, message: `the daemon answered ${res.status} with no error body`,
  }, res.status);
}

export async function call(name, params = {}, body = undefined, query = null, opts = {}) {
  const res = await send(name, params, body, "application/json", query, opts || {});
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (res.status === 401 || !res.ok || !data || data.ok === false) throw failure(res, data);
  setConnection("ok");
  return { data, status: res.status };
}

// A binary answer (the XDC export's zip): the body as a Blob. A failure still answers
// JSON, so it throws the same ApiError as call() (with error.data, e.g. the failed checks).
export async function callBlob(name, params = {}, body = undefined) {
  const res = await send(name, params, body, "application/zip, application/json");
  const type = res.headers.get("content-type") || "";
  if (res.ok && !type.startsWith("application/json")) {
    setConnection("ok");
    return res.blob();
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  throw failure(res, data);
}

// LM4: a binary GET with a query (the Live display's PNG snapshot): the body as a Blob. A
// failure still answers JSON, so it throws the same ApiError as call().
export async function callBytes(name, params = {}, query = null, accept = "application/octet-stream") {
  const res = await send(name, params, undefined, `${accept}, application/json`, query);
  const type = res.headers.get("content-type") || "";
  if (res.ok && !type.startsWith("application/json")) {
    setConnection("ok");
    return res.blob();
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  throw failure(res, data);
}

// A request harness-manager-daemon refused because a job holds the board (409 HELD, "... job <id>").
// A week-plan route this daemon does not serve yet (its lane has not landed): the catch-all
// answers 404 "no such endpoint", and a GET under /boards/{bid:path} answers 404 for the
// board id with the suffix glued on. The page then hides that feature instead of erroring.
export function routeMissing(err) {
  return !!err && err.status === 404 && (/no such endpoint/.test(err.message)
    || /\/(lease|tunnel|power|pty|baud)\b/.test(err.message));
}

// QUIET-POLL: the daemon held a background read back (nobody views the board, the lease is
// someone else's, background reads are off, or the board is busy with another client): the
// board was not touched. {kind, text, holder, retry_in_s, policy} or null.
export function quietOf(r) {
  const d = r && !r.error && r.data && r.data.data;
  return (d && d.quiet) || null;
}

export function heldByJob(err) {
  return !!err && err.errName === "HELD" && / job /.test(`${err.holder} ${err.message}`);
}

// A WebSocket the daemon refused closes with 4000 + the exit code (or fails the upgrade).
export const EXIT_NAMES = {
  1: "FAILED", 2: "USAGE", 3: "ABSENT", 4: "HELD", 5: "PORT_BOUND", 6: "ACTION_FAILED",
  7: "UNREACHABLE", 8: "ALREADY", 12: "UNAVAILABLE", 13: "NOTHING_ON_TARGET",
  14: "INCOMPATIBLE", 15: "REFUSED",
};

export function socketCloseReason(ev, opened) {
  if (ev.code >= 4000 && ev.code < 4100) {
    const code = ev.code - 4000;
    return { refused: true, code, text: `${EXIT_NAMES[code] || `exit ${code}`}${ev.reason ? `: ${ev.reason}` : ""}` };
  }
  if (!opened) return { refused: true, code: null, text: "the daemon refused the socket, or did not answer" };
  if (ev.code === 1000) return { refused: false, code: null, text: "" };
  return { refused: false, code: null, text: `connection lost (${ev.code})` };
}

// Shapes docs/API.md leaves open. The UI accepts both readings (see the T14 hand-back).
export function unwrapInfo(data) { return data && data.identity ? data : (data && data.info) || data; }
export function unwrapDebug(data) { return data && data.state ? data : (data && data.status) || data; }

// --- jobs ------------------------------------------------------------------------------------

const jobWaiters = new Map();     // job id -> {resolve, reject, onProgress, settled}
// A fast job can end, and its events arrive, before the 202 that named it: its outcome
// waits here for the waiter instead of being lost (and the poll would be the only way).
const jobOutcomes = new Map();    // job id -> {ok, value}
const OUTCOMES_KEPT = 200;

export function jobFinished(id) { return jobOutcomes.has(id); }

// FIX-PACK-4: a failed job's error names its job, so the Activity log folds the daemon's rows
// for that job (job.failed, update.failed, ...) into the one row of the action that ran it.
function jobError(id, error) {
  const e = new ApiError(error || {}, 0);
  e.job = id;
  return e;
}

export function jobEvent(ev) {
  const id = ev.data && ev.data.job;
  if (!id) return;
  if (ev.topic === "job.done" || ev.topic === "job.failed") {
    const outcome = ev.topic === "job.done" ? { ok: true, value: ev.data.result }
      : { ok: false, value: jobError(id, ev.data.error) };
    jobOutcomes.set(id, outcome);
    if (jobOutcomes.size > OUTCOMES_KEPT) jobOutcomes.delete(jobOutcomes.keys().next().value);
  }
  const w = jobWaiters.get(id);
  if (!w) return;
  if (ev.topic === "job.progress" && w.onProgress) w.onProgress(ev.data);
  if (ev.topic === "job.done") settle(id, true, ev.data.result);
  if (ev.topic === "job.failed") settle(id, false, jobError(id, ev.data.error));
}

function settle(id, ok, value) {
  const w = jobWaiters.get(id);
  if (!w || w.settled) return;
  w.settled = true;
  jobWaiters.delete(id);
  clearInterval(w.timer);
  if (ok) w.resolve(value); else w.reject(value);
}

// Resolves with the job's result, rejects with its error. Events drive it; a slow poll of
// GET /jobs/{id} is the backstop for a dropped event socket.
export function waitJob(id, { onProgress, pollMs = 1500 } = {}) {
  const known = jobOutcomes.get(id);
  if (known) return known.ok ? Promise.resolve(known.value) : Promise.reject(known.value);
  return new Promise((resolve, reject) => {
    const w = { resolve, reject, onProgress, settled: false, timer: 0 };
    jobWaiters.set(id, w);
    w.timer = setInterval(async () => {
      try {
        const { data } = await call("job", { id });
        if (data.progress && onProgress && data.state === "running") onProgress(data.progress);
        if (data.state === "done") settle(id, true, data.result);
        if (data.state === "failed") settle(id, false, jobError(id, data.error));
      } catch (e) {
        if (!e.transport && e.errName === "ABSENT") settle(id, false, e);
      }
    }, pollMs);
  });
}

// --- the event socket -------------------------------------------------------------------------

export class EventSocket {
  constructor(onEvent, onState) {
    this.onEvent = onEvent;
    this.onState = onState;
    this.ws = null;
    this.closed = false;
    this.retry = 0;
    this.timer = 0;
  }

  connect() {
    if (this.closed) return;
    let ws;
    try {
      ws = new WebSocket(socketUrl("events", {}, { topics: EVENT_TOPICS.join(",") }));
    } catch (e) {
      this.schedule();
      return;
    }
    this.ws = ws;
    ws.onopen = () => { this.retry = 0; this.onState(true); };
    ws.onmessage = (msg) => {
      if (typeof msg.data !== "string") return;
      let ev;
      try { ev = JSON.parse(msg.data); } catch (e) { return; }
      if (ev && ev.topic) this.onEvent(ev);
    };
    ws.onclose = () => {
      this.ws = null;
      this.onState(false);
      this.schedule();
    };
    ws.onerror = () => { /* onclose follows */ };
  }

  schedule() {
    if (this.closed) return;
    const delays = [500, 1000, 2000, 4000, 8000];
    const delay = delays[Math.min(this.retry, delays.length - 1)];
    this.retry += 1;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.connect(), delay);
  }

  close() {
    this.closed = true;
    clearTimeout(this.timer);
    if (this.ws) this.ws.close();
  }
}

// --- ui2 build --- (lane UI2-BUILD) The Import dialog's "Choose a zip": POST /overlays/upload
// takes the zip's bytes as the body (send() JSON-encodes every body), and `name`, `board_id`,
// `static_id`, `check_only` in the query. The answer and its failures are call()'s.
export async function callUpload(name, params = {}, bytes = null, query = null,
  type = "application/zip") {
  const url = endpointUrl(name, params);
  for (const [k, v] of Object.entries(query || {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  const headers = { Accept: "application/json", "Content-Type": type };
  if (token) headers.Authorization = `Bearer ${token}`;
  let res;
  try {
    res = await fetch(url, { method: ENDPOINTS[name][0], headers, body: bytes, cache: "no-store" });
  } catch (e) {
    setConnection("down");
    throw new ApiError({
      name: "NO_ANSWER",
      message: "harness-manager-daemon did not answer",
      hint: "check it is running: harness-manager daemon status",
    }, 0, true);
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (res.status === 401 || !res.ok || !data || data.ok === false) throw failure(res, data);
  setConnection("ok");
  return { data, status: res.status };
}
// --- end ui2 build ---
// --- bringup-usb ---
// BRINGUP-USB: the bring-up routes (docs/API.md "Bring-up over the Debug USB", bringup_api.py)
// and the card-reader routes of lane SD-FLASH it calls (cardwriter_api.py; a 404 means that
// lane is not in this build). Helpers only: ENDPOINTS is the integrator's table (CCR BRINGUP-1
// folds these names into it); tests/web/test_bringup_static.py checks each against API.md and
// the daemon's routes, as test_t14_static does for ENDPOINTS.
export const BRINGUP_ENDPOINTS = Object.freeze({
  bringupStatus: ["GET", "/bringup"],
  bringupScan: ["POST", "/bringup/scan"],
  bringupBundle: ["POST", "/bringup/bundle"],
  bringupInstall: ["POST", "/boards/{bid}/bringup/install"],
  bringupWitness: ["POST", "/boards/{bid}/bringup/witness"],
  cardwriterDevices: ["GET", "/cardwriter/devices"],       // SD-FLASH
  cardwriterWrite: ["POST", "/cardwriter/write"],          // SD-FLASH
});

// call() for a BRINGUP_ENDPOINTS name: the same token, errors and connection state.
export async function bringupCall(name, params = {}, body = undefined) {
  const [method, template] = BRINGUP_ENDPOINTS[name];
  const url = new URL(fillPath(template, params).replace(/^\//, ""), apiBase());
  const headers = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const init = { method, headers, cache: "no-store" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, init);
  } catch (e) {
    setConnection("down");
    throw new ApiError({
      name: "NO_ANSWER",
      message: "harness-manager-daemon did not answer",
      hint: "check it is running: harness-manager daemon status",
    }, 0, true);
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (res.status === 401 || !res.ok || !data || data.ok === false) throw failure(res, data);
  setConnection("ok");
  return { data, status: res.status };
}

// A route this daemon does not serve (a lane not in this build): the catch-all's 404.
export function bringupMissing(err) {
  return !!err && err.status === 404 && (/no such endpoint/.test(err.message) || err.errName === "HTTP_404");
}
// --- end bringup-usb ---
