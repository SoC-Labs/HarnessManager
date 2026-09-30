// The week-plan features (docs/API.md "Week-plan additions"): a console's PTY for `screen`
// and its baud; the hub's tunnel and lease; the board's power; clocks; update checks.
//
// Each lane (L1 hub, L2 consoles, L4 power and update) lands its routes separately. A route
// this daemon does not serve yet makes its feature `unsupported` here, and the page then
// hides it or says so calmly: nothing errors because a lane has not landed.

import { call, routeMissing, toApiError } from "./api.js";
import { clock } from "./format.js";
import { refreshState } from "./viewer.js";
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
      // LEASE-UI: a lease read that failed is not a free lease (REVIEW-W5 2: not known is not free)
      leaseError: lr.error ? `${lr.error.errName}: ${lr.error.message}` : "",
      queue: Array.isArray(d.queue) ? d.queue : [], request: d.request || null,
      incoming: Array.isArray(d.incoming) ? d.incoming : [], taken: d.taken || null,
      board: d.board || "",                          // D4: the physical board (mps3_01)
      // T8 hub mode over REST: no request messages or Keep; force only with an admin token.
      // Absent keys (a daemon or hub mode without them) mean both work.
      notesOk: d.notes_supported !== false, notesReason: d.notes_reason || "",
      revokeOk: d.can_revoke !== false, revokeReason: d.revoke_reason || "",
      // LEASE-FRESH: the hub did not answer the last read and the service answered with the
      // state it last knew ({confirmed_at, source, misses, error}); absent on a fresh read
      stale: (lease && d.stale) || null };
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

// LEASE-UI: who holds this board's hub lease, as every lease badge says it (the rail, the
// header, the Board tile, the Close dialog). Only what GET /lease last said: nothing here
// reads the hub or the board.
//   here       THIS Harness Manager holds it (it has the lease token: `lease.here`);
//   elsewhere  the same hub principal, another session or tool (`mine` without `here`):
//              every lab session shares one fpgahub principal, so `mine` is not "yours";
//   other      someone else; free: nobody; unknown: the read failed (not known is not free);
//   none       no hub; unread: not read yet.
// A daemon from before `here` (REVIEW-W5) sends only `mine`: that reads as `here`, as then.
export function leaseWho(bid) {
  const b = S.board[bid];
  const w = b && b.week;
  if (!w || (!w.hubLoaded && !w.hubUnsupported)) return { state: "unread" };
  const hub = w.hub;
  if (!hub) return { state: "none" };
  const lease = hub.lease || null;
  const req = hub.request || null;
  const kind = b.job && b.job.kind;
  const base = {
    hub, lease, host: hub.host || "", target: (lease && lease.target) || "",
    // LEASE-BOARD: fpgahub's physical board (mps3_01), when GET /lease named it
    board: (lease && lease.board) || hub.board || "",
    requested: !!req || kind === "lease_request", position: (req && req.position) || null,
    queued: !!req || !!w.leaseQueued || kind === "lease" || kind === "lease_request",
    stale: hub.stale || null,                        // LEASE-FRESH: last known, reading again
  };
  if (!lease && hub.leaseError) return { ...base, state: "unknown", holder: "", error: hub.leaseError };
  if (!lease) return { ...base, state: "free", holder: "" };
  const here = lease.here === undefined ? !!lease.mine : !!lease.here;
  if (here) return { ...base, state: "here", holder: lease.holder || "", queued: false, requested: false };
  if (lease.mine) return { ...base, state: "elsewhere", holder: lease.holder || "your hub name" };
  return { ...base, state: "other", holder: lease.holder || "someone else" };
}

// FIX-PACK-4: the ONE lease rule every gated button uses (XVC, Program, Restore baseline,
// Reset DUT, Reboot, Restart shell, Power-cycle, the DUT clock, Debug; actions.js `holder`,
// and the Update tab's lease line reads the catalogue's `here`): on a board behind a
// hub only THIS Harness Manager holding the lease (leaseWho "here") may drive it; "" when it
// may (or the board has no hub), else the one-line reason the button shows. `what` names the
// action ("XVC", "Program"). Another session of your own hub name ("elsewhere") is not you
// here: the daemon's gates say the same (services/lease.py held_here).
export function holderOnly(bid, what) {
  const who = leaseWho(bid);
  switch (who.state) {
    case "none": case "here": return "";
    case "unread": return "reading the board's hub lease first";
    case "unknown": return `${what} is for the lease holder only, and the hub lease could not be read (not known is not free)`;
    case "free": return `${what} is for the lease holder only, and nobody holds this board's lease: acquire it first (header)`;
    case "elsewhere": return `${what} is for the lease holder only: ${who.holder} holds this board in another session, not this Harness Manager`;
    default: return `${what} is for the lease holder only: ${who.holder || "someone else"} holds this board`;
  }
}

// FIX-PACK-4: the same rule on a lease object the daemon sent (GET /harness/catalog's
// board.lease): `here`, or `mine` from a daemon that predates `here`.
export function leaseHere(lease) {
  if (!lease) return false;
  return lease.here === undefined ? !!lease.mine : !!lease.here;
}

// LEASE-FRESH: the quiet note for a lease the service carried over a failed hub read ("" for a
// fresh one): when the hub last confirmed it, and that the page reads it again.
export function staleNote(stale) {
  if (!stale) return "";
  const at = epochOf(stale.confirmed_at);
  return `last confirmed ${at === null ? "a moment ago" : clock(at)}; the hub didn't answer the last read, reading again`;
}

// LEASE-BOARD: what lease text calls the leased thing: fpgahub's physical board (mps3_01)
// when GET /lease named it, else the hub target (mps3_01_pl) as before. The lease itself is
// still taken on the target (docs/HUB_MODE.md "Boards and targets"); this is the name people
// read. `who` is leaseWho's (or anything with board and target).
export function leaseName(who) {
  return (who && (who.board || who.target)) || "";
}

// The target, as a detail, only when it is not the name already ("" otherwise).
export function leaseTargetNote(who) {
  return who && who.board && who.target && who.board !== who.target ? who.target : "";
}

// "mps3_01 on mapstone-dev (target mps3_01_pl)"; "mps3_01_pl on mapstone-dev" with no board.
export function leaseWhere(who) {
  const note = leaseTargetNote(who);
  return `${leaseName(who) || "the board's target"} on ${(who && who.host) || "the hub"}${note ? ` (target ${note})` : ""}`;
}

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
      log("warning", "lease", `the lease on ${d.board || d.target || bid} was ${d.state}`, bid);
    }
    // LEASE-FRESH: our own acquire, heartbeat or release answered: that IS the lease state.
    // Show it now (the rail, the header, the Overview); the read that follows agrees, or the
    // service carries this state over a hub hiccup.
    if (d.source && !d.warning && w.hub && (d.state === "held" || d.state === "released")) {
      const held = d.state === "held" && d.here;
      w.hub = { ...w.hub, leaseError: "", stale: null,
        lease: held ? { ...(w.hub.lease || {}), target: d.target || "", board: d.board || w.hub.board || null,
          holder: d.holder || "", expires_at: d.expires_at || "", mine: true, here: true } : null };
      changed();
      if (d.source !== "heartbeat") refreshState(bid);    // the Background line moves with it
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
