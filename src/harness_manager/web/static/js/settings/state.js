// The Settings dialog's data and actions (lane SET-UI; docs/design/SETTINGS.md §5-§8, docs/API.md
// "Settings" and "Hubs in the Settings dialog"). One model, many views: every row comes from
// GET /settings/schema plus GET /settings, and every change is one PUT/DELETE whose reply is
// the resolved row. The dialog itself is UPDATE-UI's SettingsModal (selfupdate.js), sectioned.
//
// - A secret is written, never read: its row carries {set, backend, where, reachable, why}.
//   The value typed goes into PUT /settings/secrets/{key} and nowhere else (not this state,
//   not the log, not an attribute).
// - "reopen" changes offer a Reopen board button; "restart" changes join the restart banner
//   (kept per service pid in sessionStorage, so a reload keeps it and a restart clears it).
// - settings.changed (from this page, another tab or the CLI) re-reads the dialog.

import { call, routeMissing, toApiError, waitJob } from "../api.js";
import { closeBoardConsoles } from "../consoles.js";
import { hostOf } from "../format.js";
import {
  boardState, changed, log, onBoardEvent, onEventsReconnected, openedBoard, openedOrClosedHere,
  S, timed, UI_NOTE,
} from "../store.js";

// UI v2 (round 3, M7): the order the prototype lists them in.
export const SECTIONS = [
  { id: "general", label: "General", icon: "sliders-horizontal" },
  { id: "boards", label: "Boards", icon: "circuit-board" },
  { id: "hubs", label: "Hubs", icon: "server" },
  { id: "tools", label: "Tools", icon: "wrench" },
  { id: "consoles", label: "Consoles", icon: "terminal" },
  { id: "debug", label: "Debug", icon: "bug" },
  { id: "updates", label: "Updates", icon: "rocket" },
  { id: "harness-kits", label: "Harness & kits", icon: "layers" },
  { id: "advanced", label: "Advanced", icon: "file-cog" },
];
export const ADMIN_TEXT = "set by your administrator";
const SECTION_KEY = "harness_manager.settings.section";
const RESTART_KEY = "harness_manager.settings.restart";

function readSession(key, fallback) {
  try { return JSON.parse(window.sessionStorage.getItem(key) || "null") ?? fallback; } catch (e) { return fallback; }
}

function writeSession(key, value) {
  try { window.sessionStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* not kept */ }
}

export const SS = {
  // UI v2 (round 3, M7): Settings opens on General (openSettings); a link names its section
  // ("updates"). The section last used is still kept, for a reload while the dialog is open.
  section: readSession(SECTION_KEY, "general"),
  loading: false,
  loaded: false,
  error: null,             // the last read's ApiError
  schema: [],              // GET /settings/schema rows
  specs: [],               // [{parts, row}] for matching a concrete key to its declared row
  listing: null,           // GET /settings: {files, service, policy, problems, sections, instances}
  rows: {},                // key -> the resolved row (GET /settings rows, then each reply's)
  order: [],               // the keys in the listing's order
  dev: false,              // "Show developer settings" (GET /settings?all=1)
  hubs: null,              // GET /hubs: {hubs, inline, policy}
  hubsUnavailable: "",     // a daemon without /hubs (an older one): why
  busy: {},                // key -> true while its PUT/DELETE runs
  rowError: {},            // key -> the ApiError of its last change
  reopen: {},              // key -> true: changed here; applies at the next board open
  more: {},                // group id -> "show advanced rows"
  tests: {},               // "hubs:NAME" | "hubs:" (a new one) -> {running, phase, result, error, at}
  tools: {},               // tool -> {running, result, error}
  hubForm: null,           // the "Add a hub" form: {name, transport, host, url, group, jump, error, busy}
  boardForm: null,         // the "Add a board" form: {key, match, name, error, busy}
  hubAction: {},           // hub/board -> {busy, error, done} for remove / adopt / add board
  secretEdit: "",          // the key whose secret field is open for typing (never the value)
  restart: readSession(RESTART_KEY, { pid: 0, keys: [] }),
};

// --- keys: TOML dotted keys, as settings/schema.py splits them ------------------------------------

export function splitKey(key) {
  const parts = [];
  const n = key.length;
  let i = 0;
  while (i < n) {
    const c = key[i];
    if (c === '"' || c === "'") {
      let j = i + 1;
      let buf = "";
      while (j < n && key[j] !== c) {
        if (c === '"' && key[j] === "\\" && j + 1 < n && (key[j + 1] === '"' || key[j + 1] === "\\")) j += 1;
        buf += key[j];
        j += 1;
      }
      parts.push(buf);
      i = j + 1;
    } else {
      let j = i;
      while (j < n && key[j] !== ".") j += 1;
      parts.push(key.slice(i, j).trim());
      i = j;
    }
    if (key[i] === ".") i += 1;
  }
  return parts;
}

export function joinKey(parts) {
  return parts.map((p) => (p === "*" || /^[A-Za-z0-9_-]+$/.test(p) ? p
    : `"${p.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`)).join(".");
}

// The declared row (GET /settings/schema) a concrete key belongs to: exact first, then "*".
export function specOf(key) {
  const parts = splitKey(key);
  let best = null;
  for (const s of SS.specs) {
    if (s.parts.length !== parts.length) continue;
    if (s.parts.every((p, i) => p === parts[i])) return s.row;
    if (!best && s.parts.every((p, i) => p === "*" || p === parts[i])) best = s.row;
  }
  return best;
}

export function sectionOfRow(row) {
  return row.section_id || (specOf(row.key) || {}).section_id || "";
}

// --- reading --------------------------------------------------------------------------------------

function takeRows(rows, replace) {
  if (replace) {
    SS.rows = {};
    SS.order = [];
  }
  for (const r of rows || []) {
    if (!(r.key in SS.rows)) SS.order.push(r.key);
    SS.rows[r.key] = r;
  }
}

export async function loadSettings() {
  SS.loading = true;
  changed();
  const query = SS.dev ? { all: "1" } : null;
  const [sch, lst, hb] = await Promise.all([
    SS.schema.length ? Promise.resolve(null) : timed("config schema", () => call("settingsSchema")),
    timed(`config list${SS.dev ? " --all" : ""}`, () => call("settings", {}, undefined, query)),
    timed("hub list", () => call("hubs")),
  ]);
  SS.loading = false;
  if (sch && !sch.error) {
    SS.schema = sch.data.data.rows || [];
    SS.specs = SS.schema.map((row) => ({ parts: splitKey(row.key), row }));
  }
  const err = (sch && sch.error) || lst.error;
  if (err) {
    SS.error = err;
    log("error", "settings", `${lst.error ? lst.line : sch.line}  ${err.errName}: ${err.message}`);
  } else {
    SS.error = null;
    const d = lst.data.data;
    SS.listing = { files: d.files, policy: d.policy, problems: d.problems || [],
      service: d.service || { demo: false },
      sections: d.sections || [], instances: d.instances || { hubs: [], boards: [] } };
    takeRows(d.rows, true);
    SS.loaded = true;
    if (SS.restart.keys.length && SS.restart.demo !== SS.listing.service.demo) {
      SS.restart = { ...SS.restart, demo: !!SS.listing.service.demo };
      writeSession(RESTART_KEY, SS.restart);
    }
  }
  if (hb.error) {
    SS.hubs = null;
    SS.hubsUnavailable = routeMissing(hb.error) || hb.error.status === 404
      ? "this service has no /hubs routes (an older Harness Manager)" : `${hb.error.errName}: ${hb.error.message}`;
  } else {
    SS.hubs = hb.data.data;
    SS.hubsUnavailable = "";
  }
  changed();
}

export function setSection(id) {
  SS.section = id;
  writeSession(SECTION_KEY, id);
  changed();
}

export function setDev(on) {
  SS.dev = on;
  loadSettings();
}

// --- changing -------------------------------------------------------------------------------------

function afterChange(key, r) {
  const d = r.data.data;
  takeRows(d.rows, false);
  const applies = d.applies || {};
  for (const k of applies.reopen || []) SS.reopen[k] = true;
  noteRestart(applies.restart || []);
  delete SS.rowError[key];
}

async function change(key, what, fn) {
  SS.busy[key] = true;
  delete SS.rowError[key];
  changed();
  const r = await timed(what, fn);
  delete SS.busy[key];
  if (r.error) {
    SS.rowError[key] = r.error;
    log("error", "settings", `${r.line}  ${r.error.errName}: ${r.error.message}`);
  } else {
    afterChange(key, r);
    log("info", "settings", r.line);
  }
  changed();
  return r;
}

// PUT /settings {KEY: value}. A string is parsed as the CLI parses it ("90m", "a,b", "true").
export function saveSetting(key, value) {
  return change(key, `config set ${key} ${JSON.stringify(value)}`, () => call("settingsSet", {}, { [key]: value }));
}

// DELETE /settings/{key}: back to the next layer (the admin's default, the pack's, the built-in).
export function resetSetting(key) {
  return change(key, `config unset ${key}`, () => call("settingsUnset", { key }));
}

// PUT /settings/secrets/{key} {value}: the value leaves here and nowhere else.
export function storeSecret(key, value) {
  SS.secretEdit = "";
  return change(key, `config set-secret ${key}`, () => call("settingsSecretSet", { key }, { value }));
}

export function clearSecret(key) {
  return change(key, `config clear-secret ${key}`, () => call("settingsSecretUnset", { key }));
}

// --- tests: hub Test connection, Tools Detect ----------------------------------------------------

async function runTest(slot, store, body) {
  store[slot] = { running: true, phase: "", result: null, error: null, at: 0 };
  changed();
  let out = null;
  let error = null;
  try {
    const { data } = await call("settingsTest", {}, body);
    out = data.job ? await waitJob(data.job, {
      onProgress: (p) => { if (store[slot]) { store[slot].phase = p.phase || ""; changed(); } },
      pollMs: 700,
    }) : data;
  } catch (e) {
    error = toApiError(e);
  }
  store[slot] = { running: false, phase: "", result: out, error, at: Date.now() / 1000 };
  const what = `config test ${body.section}${body.name ? ` ${body.name}` : ""}`;
  if (error) log("error", "settings", `${what}  ${error.errName}: ${error.message}`);
  else log(out && out.passed ? "ok" : "warning", "settings", `${what}: ${out && out.passed ? "PASS" : `stopped at ${out && (out.failed || out.why)}`}`);
  changed();
  return store[slot];
}

export function testHub(name, table = null) {
  const body = { section: "hubs", name: name || "" };
  if (table) body.table = table;
  return runTest(`hubs:${table ? "" : name}`, SS.tests, body);
}

export function detectTool(tool) {
  return runTest(tool, SS.tools, { section: "tools", name: tool });
}

// --- hubs -----------------------------------------------------------------------------------------

async function hubStep(slot, what, fn) {
  SS.hubAction[slot] = { busy: true, error: null, done: null };
  changed();
  const r = await timed(what, fn);
  if (r.error) {
    SS.hubAction[slot] = { busy: false, error: r.error, done: null };
    log("error", "settings", `${r.line}  ${r.error.errName}: ${r.error.message}`);
  } else {
    let out = r.data.data;
    if (out.job) {
      try {
        out = await waitJob(out.job, { pollMs: 700 });
      } catch (e) {
        SS.hubAction[slot] = { busy: false, error: toApiError(e), done: null };
        changed();
        return null;
      }
    }
    SS.hubAction[slot] = { busy: false, error: null, done: out };
    log("info", "settings", r.line);
  }
  await loadSettings();
  return SS.hubAction[slot].done;
}

export async function addHub(form) {
  const values = { transport: form.transport };
  if (form.transport === "rest") {
    values.url = form.url.trim();
  } else {
    values.host = form.host.trim();
    if (form.group.trim() !== "fpga") values.group = form.group.trim();
    if (form.jump.trim()) values.jump = form.jump.trim();
  }
  form.busy = true;
  form.error = null;
  changed();
  const r = await timed(`hub add ${form.name}`, () => call("hubPut", { name: form.name.trim() }, values));
  form.busy = false;
  if (r.error) {
    form.error = r.error;
    changed();
    return false;
  }
  log("info", "settings", r.line);
  SS.hubForm = null;
  delete SS.tests["hubs:"];
  await loadSettings();
  return true;
}

export function removeHub(name, force) {
  return hubStep(`hub:${name}`, `hub remove ${name}${force ? " --force" : ""}`,
    () => call("hubRemove", { name }, undefined, force ? { force: "1" } : null));
}

export function addBoardFromHub(name, target) {
  return hubStep(`target:${name}:${target}`, `hub targets ${name} --add ${target}`,
    () => call("hubAddBoard", { name }, { target }));
}

export function adoptInline(board) {
  return hubStep(`adopt:${board}`, `hub adopt ${board}`, () => call("hubAdopt", {}, { board }));
}

export async function addBoard(form) {
  const key = form.key.trim();
  const changes = {};
  if (form.match.trim()) changes[joinKey(["boards", key, "match"])] = form.match.trim();
  if (form.name.trim()) changes[joinKey(["boards", key, "name"])] = form.name.trim();
  if (!Object.keys(changes).length) changes[joinKey(["boards", key, "via"])] = "direct";
  form.busy = true;
  form.error = null;
  changed();
  const r = await timed(`config set boards.${key} ...`, () => call("settingsSet", {}, changes));
  form.busy = false;
  if (r.error) {
    form.error = r.error;
    changed();
    return;
  }
  SS.boardForm = null;
  await loadSettings();
}

// --- boards open here: Reopen board -------------------------------------------------------------

export function openBoards() {
  return S.order.filter((bid) => S.boards[bid] && S.boards[bid].open);
}

// The open boards that boards.toml's [boards.<k>] is about: the key names the board id, or the
// board's host is in its match list.
function openBoardsOfKey(open, k) {
  const match = (SS.rows[joinKey(["boards", k, "match"])] || {}).value || [];
  return open.filter((bid) => bid === k || match.includes(hostOf(bid)) || match.includes(bid)
    || bid.includes(`@${k}`));
}

// The open boards a reopen-class key is about (SET-WIRE: its readers run when a board opens):
// "boards.<k>.*" -> that board; "hubs.<name>.*" -> the boards that use the hub; a pack's row
// ("mps3.*") -> the open boards of that pack. Anything else is about every open board.
export function boardsFor(key) {
  const open = openBoards();
  const parts = splitKey(key);
  if (parts[0] === "boards" && parts.length >= 3) return openBoardsOfKey(open, parts[1]);
  if (parts[0] === "hubs" && parts.length >= 3) {
    const hub = ((SS.hubs && SS.hubs.hubs) || []).find((h) => h.name === parts[1]);
    const keys = hub ? hub.boards || [] : [];
    return open.filter((bid) => keys.some((k) => openBoardsOfKey([bid], k).length));
  }
  const spec = specOf(key) || {};
  if (spec.pack) {
    return open.filter((bid) => {
      const cand = (S.boards[bid] || {}).candidate || {};
      return !cand.pack || cand.pack === spec.pack;
    });
  }
  return open;
}

export async function reopenBoards(bids, keys) {
  for (const bid of bids) {
    const row = S.boards[bid] || {};
    const cand = row.candidate || {};
    const c = await timed(`close ${bid}`, () => call("closeBoard", { bid }));
    log(c.error ? "error" : "info", "session", c.error ? `${c.line}  ${c.error.message}` : c.line, bid);
    if (c.error) continue;
    openedOrClosedHere(bid, false);
    S.boards[bid].holder = null;
    closeBoardConsoles(bid);
    delete S.board[bid];
    changed();
    const o = await timed(`open ${bid}`, () => call("openBoard", {}, { candidate: cand, note: UI_NOTE }));
    log(o.error ? "error" : "info", "session", o.error ? `${o.line}  ${o.error.errName}: ${o.error.message}` : o.line, bid);
    if (o.error && o.error.errName !== "ALREADY") continue;
    openedOrClosedHere(bid, true);
    const b = boardState(bid);
    const d = o.data ? o.data.data : {};
    if (d.info) {
      b.info = d.info;
      b.infoOkAt = Date.now() / 1000;
    }
    openedBoard(bid);
  }
  for (const k of keys) delete SS.reopen[k];
  changed();
}

// --- the restart banner -----------------------------------------------------------------------------

// Whether this service is the demo (its restart command differs: `daemon stop --demo`, never the
// real service's): the listing says so; kept with the pending restart for a reload.
function serviceDemo() {
  if (SS.listing && SS.listing.service) return !!SS.listing.service.demo;
  return typeof SS.restart.demo === "boolean" ? SS.restart.demo : null;
}

export function noteRestart(keys) {
  if (!keys.length) return;
  const pid = (S.daemon && S.daemon.pid) || SS.restart.pid || 0;
  const had = SS.restart.pid && SS.restart.pid === pid ? SS.restart.keys : [];
  const demo = serviceDemo();
  SS.restart = { pid, keys: [...new Set([...had, ...keys])], demo };
  writeSession(RESTART_KEY, SS.restart);
  if (demo === null && !SS.loading) loadSettings();   // learn which service this is first
  changed();
}

// Is this service the demo? (null: not known yet; the listing is on its way.) The restart note
// picks the command that restarts THIS service from it.
export function restartIsDemo() {
  return serviceDemo();
}

export function restartPending() {
  const r = SS.restart;
  if (!r.keys.length) return [];
  if (S.daemon && S.daemon.pid && r.pid && S.daemon.pid !== r.pid) return [];   // it restarted
  return r.keys;
}

export function dismissRestart() {
  SS.restart = { pid: 0, keys: [] };
  writeSession(RESTART_KEY, SS.restart);
  changed();
}

// --- events -----------------------------------------------------------------------------------------

let reloadTimer = 0;
export let settingsOpen = () => false;           // selfupdate.js says whether the dialog is open
export function whenOpen(fn) { settingsOpen = fn; }

onBoardEvent((ev) => {
  if (ev.topic !== "settings.changed") return;
  const d = ev.data || {};
  noteRestart((d.applies && d.applies.restart) || []);
  // a reopen-class change made elsewhere (the CLI, another tab, the /hubs routes) offers Reopen
  // when a board open here is one it is about (a new hub no board uses says nothing)
  for (const k of (d.applies && d.applies.reopen) || []) if (boardsFor(k).length) SS.reopen[k] = true;
  if (!settingsOpen()) return;
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(loadSettings, 150);
});

// The service came back: another pid means it restarted, and the pending restart is done.
onEventsReconnected(async () => {
  const h = await timed("health", () => call("health"));
  if (h.error) return;
  const pid = h.data.data.pid || 0;
  if (S.daemon) S.daemon.pid = pid;
  if (SS.restart.keys.length && SS.restart.pid && pid && pid !== SS.restart.pid) dismissRestart();
});
