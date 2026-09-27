// The week-plan features (docs/API.md "Week-plan additions"): a console's PTY for `screen`
// and its baud; the hub's tunnel and lease; the board's power; clocks; update checks.
//
// Each lane (L1 hub, L2 consoles, L4 power and update) lands its routes separately. A route
// this daemon does not serve yet makes its feature `unsupported` here, and the page then
// hides it or says so calmly: nothing errors because a lane has not landed.

import { call, routeMissing, toApiError } from "./api.js";
import {
  bgOpts, boardState, changed, heldBack, log, onBoardEvent, onBoardOpened, onJobEnded, S,
  scheduleRefresh, timed,
} from "./store.js";

export function week(bid) {
  const b = boardState(bid);
  if (!b.week) {
    b.week = {
      consoles: null,          // [{name, kind, baud, settable, source, reason, state, alias_of, pty}]
      pty: {},                 // name -> {path, device, command, clients} | null
      ptyError: {},            // name -> ApiError (422 on Windows, say)
      ptyUnsupported: false,
      ptyAt: {},               // name -> when the last console.pty event arrived
      baud: {},                // name -> {baud, settable, reason, choices, source}
      baudUnsupported: false,
      consoleState: {},        // name -> state from console.state events
      hub: null,               // {host, lease, tunnel} | null (not behind a hub)
      hubUnsupported: false,
      hubLoaded: false,
      leaseQueued: false,
      power: null,             // {readings, cycle_reason, device}
      powerError: null,
      powerUnsupported: false,
      powerPhases: [],
      clocks: null, clocksError: null,
      osc: null, oscError: null,
      update: null,            // the last check's result
      updateEvents: [],
      lastBackup: null,        // the BackupRecord of this page's last SD backup
    };
  }
  return b.week;
}

// --- consoles: metadata, PTYs and baud ------------------------------------------------------

export async function loadConsoleMeta(bid) {
  const w = week(bid);
  // QUIET-POLL: read when the board opens, not on a click: the rates come from the board.
  const r = await timed("consoles", () => call("consoles", { bid }, undefined, null, bgOpts(bid)));
  if (heldBack(bid, r)) return;
  if (!r.error && Array.isArray(r.data.data.consoles)) {
    w.consoles = r.data.data.consoles;
    for (const c of w.consoles) {
      // The row has the path only: read the PTY for its verbatim command (it may carry a rate).
      if (c.pty && !w.pty[c.alias_of || c.name]) loadPty(bid, c.alias_of || c.name);
    }
    changed();
  }
}

export async function loadPty(bid, name) {
  const w = week(bid);
  const asked = performance.now();
  const r = await timed(`pty ${name}`, () => call("ptyGet", { bid, name }));
  if ((w.ptyAt[name] || 0) > asked) return;     // a console.pty event is newer than this read
  if (r.error) {
    if (routeMissing(r.error)) w.ptyUnsupported = true;
  } else {
    w.pty[name] = r.data.data.pty || null;
  }
  changed();
}

// An alias row (shell for fpga_uart2) is the same console: show it once, under its target.
export function consoleRows(bid, names) {
  const w = week(bid);
  const meta = Object.fromEntries((w.consoles || []).map((c) => [c.name, c]));
  const aka = {};
  for (const c of w.consoles || []) if (c.alias_of) (aka[c.alias_of] = aka[c.alias_of] || []).push(c.name);
  return (names || []).filter((n) => !(meta[n] && meta[n].alias_of))
    .map((n) => ({ name: n, meta: meta[n] || null, aka: aka[n] || [] }));
}

// POST .../pty: create (or find) the console's PTY. `command` is used verbatim: it is
// `screen <path>`, or `screen <path> <baud>` for a serial console (screen sets 9600 otherwise).
export async function openPty(bid, name) {
  const w = week(bid);
  const { data } = await call("ptyOpen", { bid, name });
  w.pty[name] = { ...(w.pty[name] || {}), ...data };
  delete w.ptyError[name];
  changed();
  loadPty(bid, name);
  return data;
}

export async function loadBaud(bid, name) {
  const w = week(bid);
  const r = await timed(`baud ${name}`, () => call("baudGet", { bid, name }));
  if (r.error) {
    if (routeMissing(r.error)) w.baudUnsupported = true;
  } else {
    w.baud[name] = r.data.data;
  }
  changed();
}

export async function setBaud(bid, name, baud) {
  const { data } = await call("baudSet", { bid, name }, { baud });
  const w = week(bid);
  w.baud[name] = { ...(w.baud[name] || {}), baud: data.baud, source: data.source || (w.baud[name] || {}).source };
  changed();
  loadBaud(bid, name);           // read it back: the confirmation is what the daemon now says
  return data;
}

// --- the hub: tunnel and lease -------------------------------------------------------------

export async function loadHub(bid) {
  const w = week(bid);
  const [lr, tr] = await Promise.all([
    timed("lease", () => call("lease", { bid })),
    timed("tunnel", () => call("tunnel", { bid })),
  ]);
  w.hubLoaded = true;
  if (lr.error && routeMissing(lr.error)) {
    w.hubUnsupported = true;
    w.hub = null;
    changed();
    return;
  }
  const lease = lr.error ? null : lr.data.data;
  const tunnel = tr.error ? null : tr.data.data.tunnel;
  if ((!lease || !lease.hub) && !tunnel) {
    w.hub = null;                // not behind a hub
  } else {
    // docs/LEASE_REQUESTS.md adds queue, request (mine, outgoing), incoming (for my lease)
    // and taken (the last force release of my lease); an older daemon has none of them.
    const d = lease || {};
    w.hub = { host: d.hub || (tunnel && tunnel.host) || "", lease: lease ? d.lease : null, tunnel,
      queue: Array.isArray(d.queue) ? d.queue : [], request: d.request || null,
      incoming: Array.isArray(d.incoming) ? d.incoming : [], taken: d.taken || null,
      board: d.board || "",                          // D4: the physical board (mps3_01)
      // T8 hub mode over REST: no request messages or Keep; force only with an admin token.
      // Absent keys (a daemon or hub mode without them) mean both work.
      notesOk: d.notes_supported !== false, notesReason: d.notes_reason || "",
      revokeOk: d.can_revoke !== false, revokeReason: d.revoke_reason || "" };
  }
  w.hubAt = Date.now();
  for (const fn of hubHooks) {
    try { fn(bid, w.hub); } catch (e) { /* a hook never breaks the read */ }
  }
  changed();
}

// lease.js follows every read of the lease (the victim banner, the countdowns).
const hubHooks = [];
export function onHubLoaded(fn) { hubHooks.push(fn); }

// expires_at: fpgahub's ISO 8601 ("2026-09-25T12:00:00+00:00"), or epoch seconds.
export function epochOf(v) {
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  const ms = Date.parse(String(v));
  return Number.isFinite(ms) ? ms / 1000 : null;
}

export function leaseLeft(lease, now = Date.now() / 1000) {
  const at = lease ? epochOf(lease.expires_at) : null;
  return at === null ? null : Math.max(0, at - now);
}

export function durationText(s) {
  if (s === null || s === undefined) return "";
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

// --- power, clocks ---------------------------------------------------------------------------

export async function loadPower(bid) {
  const w = week(bid);
  const r = await timed("power", () => call("power", { bid }));
  if (r.error) {
    if (routeMissing(r.error)) w.powerUnsupported = true;
    else w.powerError = r.error;
  } else {
    w.power = r.data.data;
    w.powerError = null;
  }
  changed();
}

export async function loadClocks(bid) {
  const w = week(bid);
  const r = await timed("clocks", () => call("clocks", { bid }));
  w.clocksError = r.error;
  if (!r.error) w.clocks = r.data.data.readings || [];
  changed();
}

export async function loadOsc(bid) {
  const w = week(bid);
  const r = await timed("osc", () => call("osc", { bid }));
  w.oscError = r.error;
  if (!r.error) w.osc = r.data.data.readings || [];
  changed();
}

// --- events and hooks ------------------------------------------------------------------------

const hubTimers = {};
export function scheduleHub(bid, ms = 200) {
  clearTimeout(hubTimers[bid]);
  hubTimers[bid] = setTimeout(() => loadHub(bid), ms);
}

onBoardEvent((ev) => {
  const bid = ev.board_id;
  if (!bid) return;
  const d = ev.data || {};
  const w = week(bid);
  if (ev.topic === "console.pty" && d.name) {
    // {name, path, device, clients, open}: opened, closed, or a client came or left.
    w.ptyAt[d.name] = performance.now();
    const had = w.pty[d.name];
    if (d.open === false || d.closed) {
      w.pty[d.name] = null;
    } else {
      const same = had && had.path === d.path && had.command;
      w.pty[d.name] = { ...(had || {}), path: d.path, device: d.device, clients: d.clients,
        command: same ? had.command : null };
      if (!same) setTimeout(() => loadPty(bid, d.name), 0);     // the event has no command
    }
  }
  if (ev.topic === "console.state" && d.name) {
    if (d.state) w.consoleState[d.name] = d.state;
    if (d.baud !== undefined && d.baud !== null) {
      w.baud[d.name] = { ...(w.baud[d.name] || {}), baud: d.baud };
    }
  }
  if (ev.topic === "tunnel.state") {
    // {via, host, state, ports, forwards, restarts, pid, detail}: the tunnel moved.
    if (w.hub) w.hub = { ...w.hub, tunnel: { ...(w.hub.tunnel || {}), ...d } };
    else scheduleHub(bid);
  }
  if (ev.topic === "lease.state") {
    w.leaseQueued = d.state === "queued";
    if (d.state === "lost" || d.state === "expired") {
      log("warning", "lease", `the lease on ${d.target || bid} was ${d.state}`, bid);
    }
    scheduleHub(bid);
  }
  if (ev.topic === "power.cycle") {
    if (d.phase === "off") w.powerPhases = [];
    if (d.phase && !w.powerPhases.includes(d.phase)) w.powerPhases.push(d.phase);
  }
  if (ev.topic.startsWith("update.")) {
    w.updateEvents.push({ topic: ev.topic, data: d, at: ev.at });
    if (w.updateEvents.length > 100) w.updateEvents.splice(0, w.updateEvents.length - 100);
  }
});

onBoardOpened((bid) => {
  loadHub(bid);
  loadConsoleMeta(bid);
});

onJobEnded((bid, kind) => {
  if (kind === "power_cycle") { loadPower(bid); scheduleRefresh(bid, 100); }
  if (kind === "lease" || kind.startsWith("lease_")) loadHub(bid);
  if (kind === "update_harness" || kind === "update_rollback") scheduleRefresh(bid, 100);
});

// The lease and the tunnel change under us (expiry, the hub, a dropped ssh): re-read them
// now and then for every open board that is behind a hub.
setInterval(() => {
  if (document.visibilityState !== "visible") return;
  for (const [bid, row] of Object.entries(S.boards)) {
    const b = S.board[bid];
    if (row.open && b && b.week && b.week.hub) loadHub(bid);
  }
}, 30000);

export function errorText(err) {
  const e = toApiError(err);
  return `${e.errName}: ${e.message}`;
}
