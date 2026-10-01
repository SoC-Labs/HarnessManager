// The facts the Board tab's pages share (lane UI2-BOARD): the harness kind, the Debug USB
// route (G2 `mcc_route`), the OS slots (GET /slots), what the board boots from, the card-busy
// guard, and the one lease rule. Pure reads of the store plus two loaders; no page here.
//
// Nothing is invented: a fact the service has not given is "not known", with the reason.

import { call } from "../api.js";
import { usbRoute } from "../format.js";
import { bgOpts, boardState, cardJobBusy, changed, heldBack, S, timed } from "../store.js";
import { holderOnly } from "../week.js";

export function identityOf(b) { return (b && b.info && b.info.identity) || {}; }

export function isLinux(b) { return identityOf(b).harness_impl === "linux"; }

export function featuresOf(b) {
  const f = identityOf(b).features;
  return Array.isArray(f) ? f : [];
}

export function hasCap(b, name) {
  return !!(b && b.info && (b.info.capabilities || []).includes(name));
}

export function capWhy(b, name) {
  return (b && b.info && (b.info.unavailable || {})[name]) || "";
}

// --- the Debug USB (G2) -----------------------------------------------------------------------

export const USB_WORDS = {
  hub: { nav: "USB to the hub", chip: "To the hub", icon: "usb" },
  pc: { nav: "USB to this PC", chip: "To this PC", icon: "usb" },
  self: { nav: "USB looped back", chip: "Looped back into itself", icon: "repeat" },
  none: { nav: "no Debug USB", chip: "Not connected", icon: "unplug" },
  unknown: { nav: "Debug USB not known", chip: "Not known yet", icon: "circle-help" },
};

// {to: hub|pc|self|none|unknown, reason}: the open board's session says it (G2); before that
// read, the GET /boards row's; with neither (an older service), the links say it.
export function mccRoute(bid) {
  const b = boardState(bid);
  const row = S.boards[bid] || {};
  const s = b.session || {};
  if (s.mcc_route) return { to: s.mcc_route, reason: s.mcc_route_reason || "" };
  if (row.mcc_route && row.mcc_route !== "unknown") return { to: row.mcc_route, reason: row.mcc_route_reason || "" };
  const cand = (b.info && b.info.candidate) || row.candidate || {};
  const u = usbRoute(cand, row);
  return { to: u.to, reason: u.detail };
}

// --- the OS slots (GET /boards/{bid}/slots, read only) -----------------------------------------

export async function loadSlots(bid, { background = true } = {}) {
  const b = boardState(bid);
  if (b.slotsLoading) return;
  b.slotsLoading = true;
  changed();
  const r = await timed("slot status", () => call("slots", { bid }, undefined, null,
    background ? bgOpts(bid) : {}));
  b.slotsLoading = false;
  if (heldBack(bid, r)) { changed(); return; }
  if (r.error) {
    b.slotsError = r.error;
  } else {
    const d = r.data.data;
    b.slots = { available: !!d.available, reason: d.reason || "", slots: d.slots || null, at: Date.now() };
    b.slotsError = null;
  }
  changed();
}

// The slots object: GET /slots when read, else the card read's os_slots (it lacks fell_back,
// notes and each slot's boot words), else null.
export function slotsOf(b) {
  if (b.slots) return b.slots.available && b.slots.slots ? b.slots.slots : null;   // GET /slots decides
  const os = b.card && b.card.os_slots;
  return os && os.slots ? os : null;
}

// What the board boots from: "bm" (the config SD), "card" (OS slots on the user microSD),
// "netboot" (Linux with no card: the hub's TFTP image), "" (not known yet).
export function osKind(bid) {
  const b = boardState(bid);
  if (!b.info) return "";
  if (!isLinux(b)) return "bm";
  if (slotsOf(b)) return "card";
  if (b.slots && !b.slots.available) return "netboot";
  if (b.card && b.card.store && !b.card.present) return "netboot";
  return "";
}

// --- the guard: nothing resets the board while its card is written or read back -----------------

const CARD_JOBS = new Set(["harness_install", "harness_rollback", "slot_rollback", "sd_install", "sd_restore",
  "update_harness", "update_rollback", "card_commit", "card_clear"]);

// "" when a reset may go ahead, else why it must wait (SLOT-TIMING: the service refuses it too).
export function cardGuard(bid) {
  const b = boardState(bid);
  const os = slotsOf(b);
  const job = os && os.job;
  if (job && (job.busy || job.state === "writing" || job.state === "verifying")) {
    return `the user microSD is busy (${job.text || `${job.state} slot ${job.slot || "?"}`}). `
      + "A reset now can wedge the card; the service refuses a reboot meanwhile.";
  }
  if (cardJobBusy(b.card)) {
    const j = b.card.os_slots.job;
    return `the user microSD is busy (${j.text || b.cardText || `${j.state} slot ${j.slot || "?"}`}). A reset now can wedge the card.`;
  }
  if (b.job && CARD_JOBS.has(b.job.kind)) {
    return `the ${b.job.kind.replace(/_/g, " ")} job is writing the board: wait for it to end`;
  }
  return "";
}

// --- who may drive -----------------------------------------------------------------------------

// The one lease rule (week.js holderOnly): "" when this Harness Manager may drive the board.
export function driveWhy(bid, what = "Recovery") { return holderOnly(bid, what); }

// "1-4", "1, 3", "none"
export function rangeText(n) {
  if (!n.length) return "none";
  const out = [];
  let a = n[0];
  let z = n[0];
  for (const x of n.slice(1).concat([null])) {
    if (x === z + 1) { z = x; continue; }
    out.push(a === z ? `${a}` : `${a}-${z}`);
    a = x; z = x;
  }
  return out.join(", ");
}

// "3 h 12 min", "4 min", "40 s"
export function uptimeText(s) {
  if (s === null || s === undefined || !Number.isFinite(Number(s))) return "";
  const t = Math.max(0, Math.round(Number(s)));
  if (t < 90) return `${t} s`;
  const m = Math.round(t / 60);
  if (m < 90) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}
