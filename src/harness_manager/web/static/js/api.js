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
  session: ["GET", "/boards/{bid}/session"],
  helpTabs: ["GET", "/help/tabs"],
  job: ["GET", "/jobs/{id}"],
  events: ["WS", "/events"],
});

export const ADDITIVE = Object.freeze([]);

// Topics the UI follows (docs/CONTRACTS.md). console.line is left out on purpose:
// console bytes arrive on each console's own socket, so a line is never shown twice.
export const EVENT_TOPICS = [
  "board.*", "session.*", "deploy.*", "console.state", "debug.*", "controller.*",
  "storage.*", "update.*", "power.*", "job.*", "events.*",
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

export async function call(name, params = {}, body = undefined) {
  const [method] = ENDPOINTS[name];
  const url = endpointUrl(name, params);
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
  if (res.status === 401) {
    // harness-manager-daemon answers a missing or stale token with 401 and REFUSED (15).
    setConnection("auth");
    throw new ApiError((data && data.error) || {
      name: "REFUSED", code: 15, message: "session expired",
      hint: "run harness-manager ui again",
    }, 401);
  }
  setConnection("ok");
  if (!res.ok || !data || data.ok === false) {
    throw new ApiError((data && data.error) || {
      name: `HTTP_${res.status}`, message: `the daemon answered ${res.status} with no error body`,
    }, res.status);
  }
  return { data, status: res.status };
}

// A request harness-manager-daemon refused because a job holds the board (409 HELD, "... job <id>").
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

export function jobEvent(ev) {
  const id = ev.data && ev.data.job;
  if (!id) return;
  if (ev.topic === "job.done" || ev.topic === "job.failed") {
    const outcome = ev.topic === "job.done" ? { ok: true, value: ev.data.result }
      : { ok: false, value: new ApiError(ev.data.error || {}, 0) };
    jobOutcomes.set(id, outcome);
    if (jobOutcomes.size > OUTCOMES_KEPT) jobOutcomes.delete(jobOutcomes.keys().next().value);
  }
  const w = jobWaiters.get(id);
  if (!w) return;
  if (ev.topic === "job.progress" && w.onProgress) w.onProgress(ev.data);
  if (ev.topic === "job.done") settle(id, true, ev.data.result);
  if (ev.topic === "job.failed") settle(id, false, new ApiError(ev.data.error || {}, 0));
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
        if (data.state === "failed") settle(id, false, new ApiError(data.error || {}, 0));
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
