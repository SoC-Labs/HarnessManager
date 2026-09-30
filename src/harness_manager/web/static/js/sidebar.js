// The rail's board list (lane SIDEBAR-UX): the cards, their order and favourites, dragging,
// the keyboard moves, the boards.toml boards, and adding a board by address.
//
// **Order and favourites are the user's, not the browser's:** `general.board_order` and
// `general.favourite_boards` in the settings (GET /settings?section=general, PUT /settings;
// settings/rows.py). Both are lists of board ids. A board missing from the order goes after
// the ordered ones, in the order the page learnt of it. Favourites are pinned at the top in
// their own group; their order is the same list's. This browser keeps a copy only while the
// settings cannot be reached (an older service, the service down), and hands it to the
// settings when they answer again.
//
// **Moving a board** changes only its own group: the group's boards trade places among the
// slots they hold in the one order, so the other group keeps its order and a board that
// leaves the favourites goes back to where it was.
//
// **Mouse and touch** drag a card (a mouse anywhere on it, touch by its grip on the left),
// with a line where it will land. A drag never opens the board; a click without one does.
// **Keyboard:** Alt+Up / Alt+Down on a focused card moves it, and a screen reader hears
// where it went. The star (Space or Enter) favourites a board.

import { runAction } from "./actions.js";
import { ApiError, call, routeMissing } from "./api.js";
import {
  boardName, boardTitle, clock, deployBar, designText, healthOf, hexId, holderAge, holderText, LINK_ICONS,
  linkName, nameSourceText, usbRoute,
} from "./format.js";
import { AddDialogOpener } from "./add.js";
import { leaseSpecs } from "./hub.js";
import { html, useEffect, useState } from "./lib.js";
import {
  LeaseBadge, LeaseQueueList, leaveQueue, leftText, openRequestForm, previewWho, queueCount, queueOf,
  queueTitle, fullLease, readLease, requestActive,
} from "./lease.js";
import { registerHelp } from "./settings/help.js";
import { LocateButton } from "./locate.js";          // LOCATE: Identify on each board card
import {
  boardState, changed, hubBoard, log, navigate, onBoardEvent, onEventsReconnected, openedBoard,
  openedOrClosedHere, probe, S, select, setFirstBoard, timed, toast, UI_NOTE,
} from "./store.js";
import { CheckChip, Chip, Icon, MiniBar, Reason, Spinner, UsbTag } from "./ui.js";
import { epochOf, leaseLeft, leaseWho } from "./week.js";

export const ORDER_KEY = "general.board_order";
export const FAV_KEY = "general.favourite_boards";
const LOCAL_KEY = "harness_manager.sidebar";
const SAVE_MS = 250;
const DRAG_PX = 6;

// --- the preferences -----------------------------------------------------------------------

export const P = {
  order: [],            // board ids, the user's order (every board the page has seen)
  favs: [],             // board ids, the favourites
  loaded: false,
  where: "",            // "settings" | "browser" (the settings did not answer) | ""
  saving: 0,            // writes in flight
  timer: 0,             // a write waiting (debounced)
  gen: 0,               // local changes made: a read that started before one is stale
  announce: "",         // the last move or star, for screen readers
};

function readLocal() {
  try {
    const v = JSON.parse(window.localStorage.getItem(LOCAL_KEY) || "null");
    return v && Array.isArray(v.order) && Array.isArray(v.favs) ? v : null;
  } catch (e) { return null; }
}

function writeLocal(pending) {
  try {
    window.localStorage.setItem(LOCAL_KEY, JSON.stringify({ order: P.order, favs: P.favs, pending }));
  } catch (e) { /* not kept */ }
}

function clearLocal() {
  try { window.localStorage.removeItem(LOCAL_KEY); } catch (e) { /* ok */ }
}

function strings(v) { return Array.isArray(v) ? v.filter((x) => typeof x === "string" && x) : []; }

// GET /settings?section=general: the two rows. A settings service that does not answer (or
// predates them) leaves the page on this browser's copy until it does.
export async function loadPrefs() {
  if (P.timer || P.saving) return;              // our own write is on its way: it wins
  const gen = P.gen;
  try {
    const { data } = await call("settings", {}, undefined, { section: "general" });
    if (gen !== P.gen || P.timer || P.saving) return;   // moved meanwhile: the move wins
    const rows = Object.fromEntries((data.rows || []).map((r) => [r.key, r.value]));
    if (!(ORDER_KEY in rows) || !(FAV_KEY in rows)) throw new Error("no sidebar rows");
    const local = readLocal();
    if (local && local.pending) {
      // Changed while the settings were away: the user's latest, so the settings take it.
      P.order = strings(local.order);
      P.favs = strings(local.favs);
      scheduleSave(0);
    } else {
      P.order = strings(rows[ORDER_KEY]);
      P.favs = strings(rows[FAV_KEY]);
      clearLocal();
    }
    P.where = "settings";
  } catch (e) {
    if (gen !== P.gen) return;
    const local = readLocal();
    if (local) {
      P.order = strings(local.order);
      P.favs = strings(local.favs);
    }
    P.where = "browser";
    if (!(e && e.transport) && !routeMissing(e) && !/no sidebar rows/.test(String(e && e.message))) {
      log("warning", "settings", `the sidebar's order was not read (${e.message}): this browser keeps it until it is`);
    }
  }
  P.loaded = true;
  changed();
}

function scheduleSave(ms = SAVE_MS) {
  P.gen += 1;
  clearTimeout(P.timer);
  P.timer = setTimeout(savePrefs, ms);
}

async function savePrefs() {
  P.timer = 0;
  P.saving += 1;
  const body = { [ORDER_KEY]: P.order.slice(), [FAV_KEY]: P.favs.slice() };
  try {
    await call("settingsSet", {}, body);
    P.where = "settings";
    clearLocal();
  } catch (e) {
    writeLocal(true);
    if (P.where !== "browser") {
      log("warning", "settings", `the sidebar's order was not saved (${e.message}): this browser keeps it until the settings answer`);
    }
    P.where = "browser";
  } finally {
    P.saving -= 1;
    changed();
  }
}

// Another window (or `harness-manager config set`) changed them; the service restarted.
onBoardEvent((ev) => {
  if (ev.topic !== "settings.changed") return;
  const keys = (ev.data && ev.data.keys) || [];
  if (keys.includes(ORDER_KEY) || keys.includes(FAV_KEY)) loadPrefs();
});
onEventsReconnected(() => loadPrefs());

// The order is read before the page selects its first board (the top favourite). UI v2: the
// "Open a board on" setting, the hubs' leases for the rail (G3), and Help by page (lease.js
// registerHelp: after app.js's own registration, which it replaces).
export function startSidebar() {
  setFirstBoard(() => railDisplay()[0], loadPrefs());
  loadOpenOn();
  startHubReads();
  registerHelp();
}

// --- UI v2: "Open a board on" (general.open_on, G12) -------------------------------------------------
//
// workbench (the default) or overview: the tab a board lands on when it is opened here. A service
// without the row (before G12) leaves the tab the page would show anyway.

export const OPEN_ON_KEY = "general.open_on";
const OPEN_ON = { value: null };

async function loadOpenOn() {
  try {
    const { data } = await call("settings", {}, undefined, { key: OPEN_ON_KEY });
    const row = ((data && data.rows) || []).find((r) => r && r.key === OPEN_ON_KEY);
    OPEN_ON.value = row && (row.value === "workbench" || row.value === "overview") ? row.value : null;
  } catch (e) { OPEN_ON.value = null; }
}

export function openOn() { return OPEN_ON.value; }

onBoardEvent((ev) => {
  if (ev.topic === "settings.changed" && ((ev.data && ev.data.keys) || []).includes(OPEN_ON_KEY)) loadOpenOn();
});

// --- UI v2: every hub board's lease in the rail, one read per hub (G3) ------------------------------
//
// GET /hubs/{name}/leases: every target a hub serves, in ONE hub read (reused 20 s by the
// service), for the hubs the listed boards name (GET /boards `hub.name`). It also refreshes
// what the service knows (lease_known) for every board it lists. Read at start, when a new hub
// appears, and every 45 s while the page is visible; never per board.

const HUBL = {};            // hub name -> {data, error, at, loading}
const HUB_EVERY_S = 45;

export function hubLeases(name) { return HUBL[name] || null; }

function hubNames() {
  const out = new Set();
  for (const bid of S.order) {
    const h = S.boards[bid] && S.boards[bid].hub;
    if (h && h.name) out.add(h.name);
  }
  return [...out];
}

export async function readHub(name, refresh = false) {
  const slot = HUBL[name] || (HUBL[name] = { data: null, error: null, at: 0, loading: false });
  if (slot.loading) return;
  slot.loading = true;
  const r = await timed(`hub leases ${name}`, () => call("hubLeases", { name }, undefined, refresh ? { refresh: "1" } : null));
  slot.loading = false;
  slot.at = Date.now() / 1000;
  if (r.error) {
    slot.error = routeMissing(r.error) ? null : r.error;
    if (routeMissing(r.error)) slot.unsupported = true;
  } else {
    slot.data = r.data.data;
    slot.error = null;
  }
  changed();
}

function startHubReads() {
  setInterval(() => {
    if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
    const now = Date.now() / 1000;
    for (const name of hubNames()) {
      const slot = HUBL[name];
      if (slot && (slot.unsupported || slot.loading)) continue;
      if (!slot || now - slot.at > HUB_EVERY_S) readHub(name);
    }
  }, 3000);
}

// This board's target row in its hub's read, or null.
function hubTarget(bid) {
  const row = S.boards[bid] || {};
  const slot = row.hub && HUBL[row.hub.name];
  const targets = (slot && slot.data && slot.data.targets) || [];
  return targets.find((t) => (t.boards || []).includes(bid)) || null;
}

// --- the order ------------------------------------------------------------------------------

// Every listed board in the user's order: ranked ones first, the rest as the page found them.
export function railOrder() {
  const rank = new Map(P.order.map((id, i) => [id, i]));
  return S.order.map((id, i) => ({ id, i, r: rank.has(id) ? rank.get(id) : Infinity }))
    .sort((a, b) => a.r - b.r || a.i - b.i).map((x) => x.id);
}

export function railGroups() {
  const all = railOrder();
  const favs = new Set(P.favs);
  return { all, favs: all.filter((id) => favs.has(id)), rest: all.filter((id) => !favs.has(id)) };
}

// The boards as the rail shows them, top to bottom (the first is selected at start).
export function railDisplay() {
  const g = railGroups();
  return [...g.favs, ...g.rest];
}

export function isFavourite(bid) { return P.favs.includes(bid); }

// The settings row takes at most this many (settings/rows.py BOARD_LIST_MAX): the boards
// shown come first, so only long-gone ones fall off the end.
const ORDER_MAX = 500;

function persistOrder(all) {
  const shown = new Set(all);
  P.order = [...all, ...P.order.filter((id) => !shown.has(id))].slice(0, ORDER_MAX);
}

function nameOf(bid) {
  const row = S.boards[bid] || {};
  const b = S.board[bid];
  return boardName((b && b.info && b.info.candidate) || row.candidate, bid);
}

// Move a board to `to` (an index in its own group). Returns false when nothing moved.
export function moveBoard(bid, to, { announce = true } = {}) {
  const g = railGroups();
  const fav = P.favs.includes(bid);
  const group = fav ? g.favs : g.rest;
  const from = group.indexOf(bid);
  if (from < 0) return false;
  const dest = Math.max(0, Math.min(group.length - 1, to));
  if (dest === from) return false;
  const moved = group.slice();
  moved.splice(from, 1);
  moved.splice(dest, 0, bid);
  // The group's boards trade places among the slots they hold in the one order.
  const members = new Set(group);
  let k = 0;
  const all = g.all.map((id) => (members.has(id) ? moved[k++] : id));
  persistOrder(all);
  if (announce) {
    say(`${nameOf(bid)} moved to position ${dest + 1} of ${group.length}${fav ? " in Favourites" : ""}`);
  }
  scheduleSave();
  changed();
  return true;
}

export function toggleFavourite(bid) {
  const on = !P.favs.includes(bid);
  persistOrder(railOrder());          // the order as it shows now: unstarring comes back to it
  P.favs = on ? [...P.favs, bid] : P.favs.filter((id) => id !== bid);
  say(`${nameOf(bid)} ${on ? "added to" : "removed from"} Favourites`);
  scheduleSave();
  changed();
}

function say(text) {
  // The same words twice must still be heard: clear, then set on the next frame.
  P.announce = "";
  changed();
  setTimeout(() => { P.announce = text; changed(); }, 30);
}

function refocus(bid) {
  requestAnimationFrame(() => {
    const el = [...document.querySelectorAll(".board-item")].find((e) => e.dataset.board === bid);
    if (el && document.activeElement !== el) el.focus();
  });
}

function onCardKey(e, bid) {
  if (!e.altKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
  e.preventDefault();
  const g = railGroups();
  const group = P.favs.includes(bid) ? g.favs : g.rest;
  const from = group.indexOf(bid);
  if (moveBoard(bid, from + (e.key === "ArrowUp" ? -1 : 1))) refocus(bid);
  else say(`${nameOf(bid)} is already ${e.key === "ArrowUp" ? "first" : "last"}${P.favs.includes(bid) ? " in Favourites" : ""}`);
}

// --- dragging (pointer events: a mouse, a finger, a pen) --------------------------------------

export const DRAG = { bid: null, active: false, at: -1, over: null, after: false };
let pointer = null;         // {id, x0, y0, bid, group}
let swallowClick = false;   // the click that ends a drag opens nothing

function cardsOf(group, except) {
  return [...document.querySelectorAll(`.rail-card[data-group="${group}"]`)]
    .filter((el) => el.dataset.board !== except);
}

function onPointerDown(e, bid, fromGrip) {
  if (e.button !== 0 || pointer) return;
  // A finger on the card scrolls the list; a finger on the grip drags.
  if (!fromGrip && e.pointerType !== "mouse") return;
  pointer = { id: e.pointerId, x0: e.clientX, y0: e.clientY, bid,
    group: P.favs.includes(bid) ? "fav" : "rest" };
  if (fromGrip) e.preventDefault();
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
  window.addEventListener("pointercancel", onPointerCancel);
}

function onPointerMove(e) {
  if (!pointer || e.pointerId !== pointer.id) return;
  if (!DRAG.active) {
    if (Math.hypot(e.clientX - pointer.x0, e.clientY - pointer.y0) < DRAG_PX) return;
    DRAG.active = true;
    DRAG.bid = pointer.bid;
    document.body.classList.add("rail-dragging");
  }
  e.preventDefault();
  const others = cardsOf(pointer.group, pointer.bid);
  let at = 0;
  for (const el of others) {
    const r = el.getBoundingClientRect();
    if (e.clientY > r.top + r.height / 2) at += 1;
  }
  const g = railGroups();
  const group = pointer.group === "fav" ? g.favs : g.rest;
  const from = group.indexOf(pointer.bid);
  DRAG.at = at;
  if (at === from || !others.length) {
    DRAG.over = null;                   // where it is now: no line
  } else if (at < others.length) {
    DRAG.over = others[at].dataset.board;
    DRAG.after = false;
  } else {
    DRAG.over = others[others.length - 1].dataset.board;
    DRAG.after = true;
  }
  changed();
}

function endDrag() {
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
  window.removeEventListener("pointercancel", onPointerCancel);
  document.body.classList.remove("rail-dragging");
  pointer = null;
  Object.assign(DRAG, { bid: null, active: false, at: -1, over: null, after: false });
  changed();
}

function onPointerUp(e) {
  if (!pointer || e.pointerId !== pointer.id) return;
  if (DRAG.active) {
    const { bid } = pointer;
    const at = DRAG.at;
    swallowClick = true;
    setTimeout(() => { swallowClick = false; }, 0);
    endDrag();
    if (at >= 0) moveBoard(bid, at);
    return;
  }
  endDrag();
}

function onPointerCancel(e) {
  if (pointer && e.pointerId === pointer.id) endDrag();
}

// --- a board's card -----------------------------------------------------------------------------

// How the table reaches the board: "through the hub mapstone-dev", "through ssh:HOST".
export function routeText(conf) {
  if (!conf) return "";
  const via = conf.via || "";
  if (via === "hub") return `through the hub${conf.hub ? ` ${conf.hub}` : ""}`;
  if (via.startsWith("ssh:")) return `through ssh ${via.slice(4)}`;
  if (conf.hub) return `hub ${conf.hub}`;
  return "direct";
}

// UI v2: the lease on EVERY hub board. An open board shows what GET /lease said (lease.js
// LeaseBadge, the words every badge uses); a board not open, the freshest of: this page's own
// GET /lease of it (the preview), its hub's read (G3, one per hub), or what the service last knew
// (GET /boards lease_known, no hub call); else that it has not been read. "+N waiting": the
// hub's queue, where the page has read it.
function knownWords(k) {
  const name = k.board || k.target || "the board";
  if (k.state === "free") return { state: "free", level: "", icon: "lock-open", text: "Free", title: `Free: nobody held ${name}` };
  if (k.here) return { state: "here", level: "ok", icon: "user", text: "Yours", title: `Your hub lease on ${name}, held by this Harness Manager` };
  if (k.mine) {
    return { state: "elsewhere", level: "held", icon: "lock", text: `Held by ${k.holder || "your hub name"} (another session)`,
      title: `Held under your hub name by another session or tool, not this Harness Manager` };
  }
  return { state: "other", level: "held", icon: "lock", text: `Held by ${k.holder || "someone else"}`, title: `Held by ${k.holder || "someone else"}` };
}

// The lease of a board that is not open, as the rail and the preview show it: {k (knownWords'
// input), at, source, waiting, requested, position} or null when nothing was read.
export function closedLease(bid) {
  const row = S.boards[bid] || {};
  const w = previewWho(bid);
  const t = hubTarget(bid);
  const tRead = t ? (HUBL[row.hub.name] || {}).at || 0 : 0;       // this page's clock, as w.at
  const tAt = t ? epochOf(t.confirmed_at) || tRead : 0;
  const q = queueOf(bid);
  if (w.source === "read" && (!t || (w.at || 0) >= tRead)) {
    const L = w.lease || {};
    return { k: { state: w.state === "free" ? "free" : "held", holder: w.holder, here: w.state === "here",
      mine: w.state === "here" || w.state === "elsewhere", board: w.board, target: w.target, hub: w.host },
    at: w.at, source: "read", waiting: queueCount(q), requested: !!w.request, position: w.position, expires: L.expires_at };
  }
  if (t) {
    return { k: { state: t.state, holder: t.holder, here: !!t.here, mine: !!t.mine, board: t.board || "", target: t.target,
      hub: row.hub.name }, at: tAt, source: "hub", waiting: t.queue_length || 0, requested: false, position: null,
    expires: t.expires_at };
  }
  const k = row.lease_known;
  if (k) {
    return { k, at: epochOf(k.confirmed_at), source: "known", waiting: k.queue_length || 0, requested: false,
      position: null, expires: k.expires_at };
  }
  return null;
}

function KnownLease({ bid }) {
  const c = closedLease(bid);
  if (!c) {
    return html`<div class="board-lease" data-testid="rail-lease-row" data-lease="unread" data-source="none">
      <${Chip} level="unk" icon="circle-help" cls="lease-badge" testid="rail-lease-badge"
        title="Behind a hub: its lease is read when you open the board">Lease not read<//></div>`;
  }
  const w = knownWords(c.k);
  const title = `${w.title} (${c.k.hub || "the hub"}), as this Harness Manager last read it${c.at ? ` at ${clock(c.at)}` : ""}${c.source === "hub" ? " (the hub's read of every board)" : ""}.`;
  return html`<div class="board-lease" data-testid="rail-lease-row" data-lease=${w.state} data-source=${c.source === "known" ? "known" : c.source}>
    <${Chip} level=${w.level} icon=${w.icon} cls="lease-badge" testid="rail-lease-badge" title=${title}>
      <span class="lease-badge-text">${w.text}</span><//>
    ${c.requested ? html`<${Chip} level="accent" icon="send" cls="lease-badge" testid="rail-lease-queued"
      title="Your request is in the hub's queue">Requested${c.position ? ` · #${c.position}` : ""}<//>` : null}</div>`;
}

function RailLease({ bid, row }) {
  const who = row.open ? leaseWho(bid).state : "unread";
  if (who === "none") return null;                  // read: not behind a hub
  const read = who !== "unread";
  if (!read && !hubBoard(bid)) return null;
  const q = queueOf(bid);
  const c = !read ? closedLease(bid) : null;
  const n = read ? queueCount(q) : c ? (c.source === "read" ? queueCount(q) : c.waiting) : 0;
  const title = q && n === queueCount(q) ? queueTitle(q) : `${n} waiting for the hub lease (the hub's count)`;
  return html`<div class="rail-lease">
    ${read ? html`<${LeaseBadge} bid=${bid} />` : html`<${KnownLease} bid=${bid} />`}
    ${n ? html`<span class="lq-more" data-testid="rail-lease-waiting" title=${`Waiting for the hub lease:\n${title}`}>
      +${n} waiting</span>` : null}
  </div>`;
}

// The kind of harness, when the board said it (identity.harness_impl).
function kindText(ident) {
  const impl = ident && ident.harness_impl;
  if (!impl) return "";
  return impl === "linux" ? "Linux" : impl === "bare-metal" || impl === "baremetal" ? "bare-metal" : impl;
}

function BoardCard({ bid, group, index, count }) {
  const row = S.boards[bid] || {};
  const cand = row.candidate || {};
  const b = S.board[bid];
  const ident = (b && b.info && b.info.identity) || cand.identity || null;
  const named = (b && b.info && b.info.candidate) || cand;     // N1: the latest name
  const design = designText(ident);
  const shell = ident && ident.shell_id;
  const mine = !!row.open;
  const held = !mine && row.holder;
  const fromConfig = row.source === "config" && !mine;         // listed from boards.toml only
  const conf = row.configured || null;
  let dot = "unk";
  let dotTitle = "not checked";
  if (mine && b && b.info) {
    const h = healthOf(b.info);
    dot = h.level;
    dotTitle = `${h.text}: ${h.detail}`;
  } else if (fromConfig) {
    dotTitle = `in boards.toml (${conf ? conf.key : "?"}): not contacted until you open it`;
  } else if (cand.evidence) {
    dot = "ok";
    dotTitle = `found: ${cand.evidence}`;
  }
  const kinds = [...new Set((cand.links || []).map((l) => l.kind))];
  const kind = kindText(ident);
  const bar = b ? deployBar(b.deploy) : null;           // the mini download bar (deploy.progress)
  const fav = P.favs.includes(bid);
  const name = boardName(named, bid);
  const cls = ["rail-card", DRAG.bid === bid ? "dragging" : "",
    DRAG.over === bid ? (DRAG.after ? "drop-after" : "drop-before") : ""].join(" ");
  const click = (e) => {
    if (swallowClick) { e.preventDefault(); return; }
    select(bid);
  };
  return html`<li class=${cls} data-board=${bid} data-group=${group} aria-posinset=${index + 1} aria-setsize=${count}>
    <span class="rail-grip" aria-hidden="true" title="Drag to reorder (or Alt+Up / Alt+Down)"
      onPointerDown=${(e) => onPointerDown(e, bid, true)}><${Icon} name="grip-vertical" cls="sm" /></span>
    <button type="button" class="board-item" aria-current=${S.selected === bid ? "true" : "false"}
      data-board=${bid} onClick=${click} onKeyDown=${(e) => onCardKey(e, bid)}
      onPointerDown=${(e) => onPointerDown(e, bid, false)}
      aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown" aria-describedby="rail-move-help">
      <div class="board-row1">
        <span class=${`dot ${dot}`} title=${dotTitle}></span>
        <span class="board-name" data-testid="rail-name"
          title=${named.name ? `${bid} · ${nameSourceText(named)}` : bid}>${name}</span>
        ${(b && b.job) || (row.job && !(b && b.readOnOpen)) ? html`<span class="i-muted" title="a job is running on this board"><${Spinner} /></span>` : null}
        ${mine ? html`<${Chip} level="accent" icon="plug-zap" cls="lock-chip" testid="rail-open"
            title="Open in this Harness Manager: its board lock is this daemon's (the hub lease is the line below)">Open<//>`
          : held ? html`<${Chip} level="warn" icon="lock" cls="lock-chip" title=${`held by ${holderText(row.holder)}`}>
              ${row.holder.user || "held"}<//>` : null}
      </div>
      <${RailLease} bid=${bid} row=${row} />
      <div class="board-row2" title=${shell ? `The design in the partition, on shell ${shell}` : "The design in the partition"}>
        <${Icon} name=${bar ? "loader-circle" : "layers"} cls=${bar ? "sm spin" : "sm"} />
        <span class="pack">${(cand.pack || String(bid).split("@")[0] || "").toUpperCase()}</span>
        ${design ? html`<span class="dsg">${design}</span>` : fromConfig ? html`<span class="muted" data-testid="rail-not-open">not open</span>`
          : html`<span class="muted">design not read</span>`}
        ${kind ? html`<span class="knd">· ${kind}</span>` : null}
      </div>
      ${bar ? html`<div class="pgm-rail" data-testid="rail-progress" title=${`Programming ${bar.overlay}: ${bar.line}`}>
        <${MiniBar} bar=${bar} /><span class="pgm-rail-t">${bar.phase}${bar.pct ? ` ${bar.pct}` : ""}</span></div>` : null}
      <div class="board-row3">
        ${kinds.map((k) => html`<span key=${k} title=${linkName(k)}><${Icon} name=${LINK_ICONS[k] || "link"} cls="sm" /></span>`)}
        <span class="links-text">${conf && conf.via ? html`<span data-testid="rail-route" title=${`boards.toml ${conf.key}`}>${routeText(conf)}</span>`
          : kinds.map(linkName).join(" · ")}</span>
        <${UsbTag} usb=${usbRoute(named, row)} testid="rail-usb" />
      </div>
    </button>
    <button type="button" class=${`rail-star ${fav ? "on" : ""}`} aria-pressed=${fav ? "true" : "false"}
      data-testid="rail-star" aria-label=${`Favourite ${name}`}
      title=${fav ? "Remove from favourites" : "Add to favourites: pinned at the top"}
      onClick=${() => toggleFavourite(bid)}><${Icon} name="star" cls="sm" /></button>
    <${LocateButton} bid=${bid} where="rail" />
  </li>`;
}

function Group({ id, label, ids }) {
  return html`<ul class="board-group" data-testid=${`rail-group-${id}`} aria-label=${label}>
    ${ids.map((bid, i) => html`<${BoardCard} key=${bid} bid=${bid} group=${id} index=${i} count=${ids.length} />`)}
  </ul>`;
}

export function BoardList() {
  const g = railGroups();
  return html`<div class="board-list">
    ${g.favs.length ? html`<div class="rail-group-title" id="rail-fav-title"><${Icon} name="star" cls="sm" />Favourites</div>
      <${Group} id="fav" label="Favourite boards" ids=${g.favs} />
      ${g.rest.length ? html`<div class="rail-group-title">Other boards</div>` : null}` : null}
    <${Group} id="rest" label=${g.favs.length ? "Other boards" : "Boards"} ids=${g.rest} />
    <p id="rail-move-help" class="sr-only">Alt+Up or Alt+Down moves this board in the list.</p>
    <div class="sr-only" role="status" aria-live="polite" data-testid="rail-announce">${P.announce}</div>
  </div>`;
}

// --- the boards.toml boards: after a scan, and for an address typed into Add -----------------------

// The listed boards boards.toml configures and this service has not opened.
export function configuredBoards() {
  return railDisplay().filter((bid) => {
    const row = S.boards[bid];
    return row && row.configured && !row.open;
  });
}

// What a Scan offers beside the boards that answered: the boards.toml boards, not contacted.
export function ScanOffer() {
  const ids = S.scan.running || !S.scan.line ? [] : S.scan.offer || [];
  if (!ids.length) return null;
  return html`<div class="rail-offer" data-testid="scan-offer">
    <span class="muted">In boards.toml:</span>
    ${ids.filter((bid) => S.boards[bid]).map((bid) => html`<button type="button" key=${bid} class="btn ghost sm"
      data-offer=${bid} onClick=${() => select(bid)} title=${`${bid}: ${routeText(S.boards[bid].configured)}`}>
      <${Icon} name="server" cls="sm" /><span>${nameOf(bid)}${" "}<span class="muted small">${routeText(S.boards[bid].configured)}</span></span></button>`)}
  </div>`;
}

function hostPort(text) {
  let s = String(text || "").trim();
  const at = s.indexOf("@");
  if (at >= 0) s = s.slice(at + 1);
  const m = /^\[?([^\]]*?)\]?(?::(\d+))?$/.exec(s);
  return m ? { host: m[1].toLowerCase(), port: m[2] || "" } : { host: s.toLowerCase(), port: "" };
}

function sameBoard(a, b) {
  return !!a.host && a.host === b.host && (!a.port || !b.port || a.port === b.port);
}

// The boards.toml entry an address names (its key, a match, its board id or its Ethernet
// link), or null.
export function configFor(address) {
  const want = hostPort(address);
  if (!want.host) return null;
  for (const bid of S.order) {
    const row = S.boards[bid];
    const conf = row && row.configured;
    if (!conf) continue;
    const refs = [bid, conf.key, ...(conf.match || []),
      ...((row.candidate && row.candidate.links) || []).filter((l) => l.kind === "ethernet").map((l) => l.address)];
    if (refs.some((r) => sameBoard(want, hostPort(r)))) return { bid, conf };
  }
  return null;
}

// The route to send with a probe: "hub", "ssh:HOST", or "" (direct / the board's own).
export function viaOf(text) {
  const t = String(text || "").trim();
  if (!t) return "";
  if (t === "hub" || t.startsWith("ssh:")) return t;
  return `ssh:${t}`;
}

// The rail's + (app.js) renders this: UI v2 has ONE Add a board dialog (add.js), which opens
// on By address from here (the button says "Add a board by address"), with From a hub a click
// away. The rail's own inline form is gone.
export function AddByAddress({ onDone }) {
  return html`<${AddDialogOpener} onDone=${onDone} mode="addr" />`;
}

// --- UI v2: the preview of a board that is not open (round 3, plan S20-S22) ----------------------
//
// What the page knows without opening it: the hub lease and its queue (GET /boards/{bid}/lease,
// G3: a board that is not open is read too), the design it last reported, how it is reached;
// and the ways in. A board with no hub: Open board. A hub board nobody holds: Open and take the
// lease, or Open to watch. One someone else holds: Request board (G3: without opening it: the
// holder is asked, nothing is locked), or Cancel request; and Open to watch. `data-action="open"`
// is always the plain open (the tests' and the CLI's), which takes no lease.

// FIX-PACK-4: the preview's "Lock" was the service's own board lock, and read "free" for a board
// alice holds on the hub. It is named for what it is, and the hub lease has its own row.
const LOCK_TITLE = "Harness Manager's own lock on this board: free unless another Harness Manager "
  + "session or tool on this machine has it open. The hub lease is its own row.";

const PREVIEW_EVERY_MS = 20000;
const READY_MS = 2500;

// POST /boards: open it here (this daemon's board lock), then read what the workspace shows.
// `take`: then acquire the hub lease (the acquire job; it may queue). Resolves to the timed()
// result of the open; the board stays closed on an error.
export async function openBoardHere(bid, { take = false } = {}) {
  const row = S.boards[bid] || {};
  const cand = row.candidate || {};
  const r = await timed(`open ${bid}`, () => call("openBoard", {}, { candidate: cand, note: UI_NOTE }));
  const already = r.error && r.error.errName === "ALREADY";   // open in this daemon: use it
  log(r.error && !already ? "error" : "info", "session",
    r.error ? `${r.line}  ${r.error.errName}: ${r.error.message}` : r.line, bid);
  if (r.error && !already) {
    if (r.error.holder) S.boards[bid] = { ...S.boards[bid], holder: { user: r.error.holder } };
    changed();
    return r;
  }
  openedOrClosedHere(bid, true);
  const b = boardState(bid);
  const d = r.data ? r.data.data : {};
  if (d.info) {
    b.info = d.info;
    b.infoOkAt = Date.now() / 1000;
  }
  // The session is open even when the first read failed; the workspace says why.
  if (!d.info && d.info_error) b.infoError = new ApiError(d.info_error, 200);
  openedBoard(bid);
  // general.open_on: the tab a board opens on, unless this session already chose one for it (a
  // deep link, a reload, the tab it was on when it was closed): that is where you were.
  const tab = openOn();
  if (tab && !S.sections[bid]) navigate(bid, tab);
  if (take) {
    const spec = leaseSpecs(bid).acquire;
    runAction(bid, "lease", spec).then((res) => {
      if (res && res.ok) toast(`${boardName(cand, bid)}: the hub lease is yours`, { icon: "user" });
      else if (res) toast(`${boardName(cand, bid)} is open to watch: the lease was not taken (Activity says why)`, { icon: "triangle-alert", level: "err" });
    });
  }
  changed();
  return r;
}

function previewChip(w, left) {
  if (w.state === "here") return { level: "accent", icon: "user", text: `Yours${left !== null ? ` · ${leftText(left)}` : ""}` };
  if (w.state === "free") return { level: "", icon: "lock-open", text: "Free" };
  if (w.state === "elsewhere") return { level: "held", icon: "lock", text: `Held by ${w.holder} (another session)` };
  if (w.state === "other") return { level: "held", icon: "lock", text: `Held by ${w.holder}${left !== null ? ` · ${leftText(left)}` : ""}` };
  if (w.state === "unknown") return { level: "unk", icon: "circle-help", text: "Lease unknown" };
  return { level: "unk", icon: "circle-help", text: "Not read yet" };
}

function PreviewLease({ bid, w }) {
  const q = queueOf(bid);
  const c = closedLease(bid);
  const n = q ? queueCount(q) : c ? c.waiting : 0;
  const left = leaseLeft(w.lease);
  const chip = previewChip(w, left);
  const at = w.at || (c && c.at) || null;
  const title = w.state === "unread" ? "Read from the hub in a moment (no board is contacted)"
    : `${chip.text}: what this Harness Manager last read of the lease on ${w.board || w.target || "the board"} (${w.host || "the hub"})${at ? ` at ${clock(at)}` : ""}${w.error ? `; the last read failed: ${w.error}` : ""}.`;
  const req = w.request;
  return html`<dt>Hub lease</dt><dd data-testid="preview-lease" data-lease=${w.state} data-source=${w.source || "none"}>
    <div class="line"><${Chip} level=${chip.level} icon=${chip.icon} testid="preview-lease-chip" title=${title}>${chip.text}<//>
      ${req ? html`<${Chip} level="accent" icon="send" testid="preview-requested"
        title=${`Your request is in the hub's queue${req.position ? `: #${req.position} of the people waiting` : ""}${req.want_s ? `, for ${leftText(req.want_s)}` : ""}`}>Requested${req.position ? ` · #${req.position}` : ""}<//>` : null}
      <span class="sub" data-testid="preview-waiting">${n ? `${n} waiting` : w.state === "unread" || w.state === "unknown" ? "" : "nobody waiting"}</span>
      ${at ? html`<span class="muted small" data-testid="preview-lease-at">as of ${clock(at)}</span>` : null}</div>
    ${w.error && w.source !== "read" ? html`<div class="sub">the last read failed: ${w.error}</div>` : null}
    ${q && n ? html`<div class="lq-preview"><${LeaseQueueList} q=${q} testid="preview-queue" /></div>` : null}</dd>`;
}

export function BoardPreview({ bid }) {
  const row = S.boards[bid] || {};
  const cand = row.candidate || {};
  const ident = cand.identity || null;
  const [st, setSt] = useState({ busy: "", line: "", error: null });
  const hub = hubBoard(bid);
  // The buttons wait for the first read of the lease (at most READY_MS): they depend on it, and
  // a button that turns into another under the pointer ("Open board" into "Open and take the
  // lease") would take a lease nobody asked for.
  const [ready, setReady] = useState(!hub);
  useEffect(() => {
    if (!hub) { setReady(true); return undefined; }
    let live = true;
    setReady(!!(fullLease(bid) && (fullLease(bid).data || fullLease(bid).error)));
    readLease(bid).then(() => { if (live) setReady(true); });
    const cap = setTimeout(() => { if (live) setReady(true); }, READY_MS);
    const t = setInterval(() => { if (document.visibilityState === "visible") readLease(bid); }, PREVIEW_EVERY_MS);
    return () => { live = false; clearTimeout(cap); clearInterval(t); };
  }, [bid, hub]);
  const w = hub ? previewWho(bid) : { state: "none" };
  const open = async (take) => {
    setSt({ busy: take ? "take" : "watch", line: "", error: null });
    const r = await openBoardHere(bid, { take });
    const failed = r.error && r.error.errName !== "ALREADY";
    setSt({ busy: "", line: r.line, error: failed ? r.error : null });
  };
  const name = boardName(cand, bid);
  const held = row.holder;
  const fromConfig = row.source === "config";       // SIDEBAR-UX: listed from boards.toml
  const conf = row.configured || null;
  const kind = ident && ident.harness_impl ? (ident.harness_impl === "linux" ? "Linux" : "bare-metal") : "";
  const hubInfo = row.hub || null;
  const target = (hubInfo && hubInfo.target) || (conf && conf.target) || w.target || "";
  const hubHost = (hubInfo && hubInfo.host) || w.host || (conf && conf.hub) || "";
  const requesting = requestActive(bid) || !!w.request;
  const busy = st.busy;
  const openBtn = (primary, label = "Open to watch", icon = "eye") => html`<button type="button" key="open"
      class=${`btn ${primary ? "primary" : "ghost"}`} data-action="open" onClick=${() => open(false)}
      aria-busy=${busy === "watch" ? "true" : undefined} disabled=${!!busy}>
    ${busy === "watch" ? html`<${Spinner} /> Opening...` : html`<${Icon} name=${icon} /> ${label}`}</button>`;
  let acts;
  let note = "";
  let noteLevel = "";
  if (!hub || w.state === "none") {
    acts = openBtn(true, "Open board", "lock-open");
  } else if (w.state === "free") {
    acts = html`<button type="button" key="take" class="btn primary" data-action="open-take" onClick=${() => open(true)}
        aria-busy=${busy === "take" ? "true" : undefined} disabled=${!!busy}
        title="Open the board here and take its hub lease (60 min, renewed while it is open)">
        ${busy === "take" ? html`<${Spinner} /> Opening...` : html`<${Icon} name="user" /> Open and take the lease`}</button>
      ${openBtn(false)}`;
  } else if (w.state === "other") {
    acts = requesting
      ? html`<button type="button" key="cancel" class="btn" data-action="preview-cancel-request" onClick=${() => leaveQueue(bid)}>
          <${Icon} name="x" /> Cancel request</button>${openBtn(false)}`
      : html`<button type="button" key="request" class="btn primary" data-action="preview-request" aria-haspopup="dialog"
          onClick=${(e) => openRequestForm(bid, e.currentTarget)}><${Icon} name="send" /> Request board</button>${openBtn(false)}`;
    note = requesting
      ? `You are #${(w.request && w.request.position) || "?"} in the hub's queue. ${w.holder.split("@")[0]} sees your request in their Harness Manager and on the board's front panel.`
      : `Requesting does not open or lock the board. ${w.holder.split("@")[0]} sees it in their Harness Manager and on the front panel.`;
    noteLevel = "held";
  } else if (w.state === "elsewhere") {
    acts = openBtn(true);
    note = `${w.holder} holds the lease in another session (a soak or a runner), not this Harness Manager. Watching shows its consoles and front panel; Program, Reset DUT and debug stay off here.`;
    noteLevel = "held";
  } else if (w.state === "here") {
    acts = openBtn(true, "Open board", "lock-open");
    note = "The hub lease is yours already (kept when the board was closed): open the board to keep renewing it.";
  } else {
    acts = openBtn(true, "Open board", "lock-open");
    note = w.state === "unknown" ? `The hub lease could not be read (${w.error}): not known is not free.` : "";
  }
  const links = cand.links || [];
  const eth = links.filter((l) => l.kind === "ethernet").map((l) => l.address);
  // how Harness Manager reaches the harness: the MCC's own link (hub-mcc://) is the Debug USB's
  const reach = links.filter((l) => !String(l.address || "").startsWith("hub-mcc://"));
  return html`<div class="section-body"><div class="preview stack">
    <section class="card preview-card" aria-label="Board" data-testid="preview" data-lease=${w.state}>
      <div class="card-head"><h2 class="card-title" data-testid="preview-name"><${Icon} name=${hub ? "server" : "ethernet-port"} />${cand.name
        ? `${cand.name} · ${cand.label || boardTitle(cand, bid)}` : cand.label || boardTitle(cand, bid)}</h2>
        <span class="spacer"></span><${LocateButton} bid=${bid} where="preview" /></div>
      <p class="card-sub">${S.packs[cand.pack] || cand.pack || "Board"}${kind ? ` · ${kind} harness` : ""}${hub && target
        ? html` · hub target <span class="mono">${target}</span>${hubHost ? ` on ${hubHost}` : ""}` : eth.length ? html` · <span class="mono">${eth[0]}</span> on this network` : ""}${fromConfig
        ? " · in boards.toml: not contacted until you open it" : ""}</p>
      <div class="card-body">
        <dl class="kv">
          ${hub ? html`<${PreviewLease} bid=${bid} w=${w} />` : null}
          <dt>Loaded</dt><dd data-testid="preview-loaded">${ident ? html`<div class="line"><span>${ident.rm_name || "unknown"}${" "}
              <span class="mono sub">${hexId(ident.rm_id)}</span></span>
              <span class="sub">on shell ${hexId(ident.shell_id) || "?"}${ident.harness_version ? ` · harness firmware ${ident.harness_version}` : ""}</span>
              <${CheckChip} check=${ident.build_check} testid="preview-build" prefix="build " /></div>
              <div class="sub">as the board last reported it: read again when you open it</div>`
            : html`<span class="muted">read when you open it</span>`}</dd>
          <dt>Found</dt><dd><div class="line">${fromConfig ? html`<${Chip} level="unk" icon="circle-help">not contacted<//>`
            : html`<${Chip} level="ok" icon="circle-check">found<//>`}
            ${cand.evidence ? html`<span class="sub">${cand.evidence}</span>` : null}</div></dd>
          <dt>Reached by</dt><dd>${reach.map((l) => html`<div key=${l.kind + l.address} class="mono small" data-link=${l.kind}>${linkName(l.kind)} ${l.address}${l.via ? ` · ${l.via === "hub" ? "through the hub's ssh tunnel" : `via ${l.via}`}` : hub && l.kind === "ethernet" && row.hub ? " · through the hub's ssh tunnel" : ""}</div>`)}
            ${conf ? html`<div data-testid="preview-route" class="small">boards.toml <b>${conf.key}</b>: ${routeText(conf)}${conf.target ? html` <span class="mono sub">${conf.target}</span>` : null}</div>` : null}
            ${!reach.length && !conf ? html`<span class="muted">no link known</span>` : null}</dd>
          <dt title=${LOCK_TITLE}>This app's lock</dt><dd data-testid="preview-lock">${held ? html`<${Chip} level="warn" icon="lock" title=${LOCK_TITLE}>held by ${holderText(held)}${held.since ? `, ${holderAge(held)}` : ""}<//>`
            : html`<${Chip} icon="lock-open" title=${LOCK_TITLE}>free<//>`}</dd>
        </dl>
        <div class="open-row" data-testid="preview-actions">${ready ? acts
          : html`<span class="muted" data-testid="preview-reading"><${Spinner} /> Reading the hub lease…</span>`}</div>
        <div class="preview-notes">
          ${note ? html`<${Reason} level=${noteLevel} text=${note} testid="preview-note" />` : null}
          <${Reason} text=${held ? "Open asks the daemon anyway: it refuses a live lock and takes over a stale one."
            : hub ? "Opening takes this app's lock on the board, so the CLI and this page share one session. The hub lease is separate: it decides who may program and reset."
            : `Opening takes the board's lock for this daemon, so the CLI and this page share one session. ${name} stays yours until you close it.`} />
        </div>
        ${st.line ? html`<div class="result" data-testid="open-result">
          <div><span class=${`rc ${st.error ? "err" : "ok"}`}>${st.line}</span></div>
          ${st.error ? html`<div><span class="errname">${st.error.errName}</span>  ${st.error.message}</div>
            ${st.error.hint ? html`<div class="hint">hint: ${st.error.hint}</div>` : null}
            <div class="hint">The board was not opened.</div>` : null}
        </div>` : null}
      </div>
    </section>
  </div></div>`;
}
