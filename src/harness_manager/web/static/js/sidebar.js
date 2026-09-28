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

import { call, routeMissing } from "./api.js";
import {
  boardName, designText, healthOf, holderText, LINK_ICONS, linkName, nameSourceText,
} from "./format.js";
import { html, useState } from "./lib.js";
import { LeaseBadge } from "./lease.js";
import {
  changed, log, onBoardEvent, onEventsReconnected, probe, S, select, setFirstBoard,
} from "./store.js";
import { Chip, Icon, Spinner } from "./ui.js";

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

// The order is read before the page selects its first board (the top favourite).
export function startSidebar() { setFirstBoard(() => railDisplay()[0], loadPrefs()); }

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
      ${mine ? html`<${LeaseBadge} bid=${bid} />` : null}
      <div class="board-row2">
        <span class="pack">${(cand.pack || String(bid).split("@")[0] || "").toUpperCase()}</span>
        ${design ? html` · ${design}` : fromConfig ? html` · <span class="muted" data-testid="rail-not-open">not open</span>`
          : html` · <span class="muted">design not read</span>`}
        ${shell ? html` · <span class="mono">${shell}</span>` : null}
      </div>
      <div class="board-row3">
        ${kinds.map((k) => html`<span key=${k} title=${linkName(k)}><${Icon} name=${LINK_ICONS[k] || "link"} cls="sm" /></span>`)}
        <span class="links-text">${conf && conf.via ? html`<span data-testid="rail-route" title=${`boards.toml ${conf.key}`}>${routeText(conf)}</span>`
          : kinds.map(linkName).join(" · ")}</span>
      </div>
    </button>
    <button type="button" class=${`rail-star ${fav ? "on" : ""}`} aria-pressed=${fav ? "true" : "false"}
      data-testid="rail-star" aria-label=${`Favourite ${name}`}
      title=${fav ? "Remove from favourites" : "Add to favourites: pinned at the top"}
      onClick=${() => toggleFavourite(bid)}><${Icon} name="star" cls="sm" /></button>
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

function viaDefault(conf) {
  const via = (conf && conf.via) || "";
  return via === "hub" || via.startsWith("ssh:") ? via : "";
}

// A board by address, optionally through a hub (L1: POST /probe {via}). An address that a
// boards.toml entry names takes that entry's route unless another is typed (SIDEBAR-UX).
export function AddByAddress({ onDone }) {
  const [value, setValue] = useState("");
  const [hub, setHub] = useState("");
  const [typed, setTyped] = useState(false);       // the user typed a route: it wins
  const match = configFor(value);
  const route = typed ? hub : viaDefault(match && match.conf);
  const submit = (e) => {
    e.preventDefault();
    const host = value.trim();
    if (!host) return;
    probe([host], viaOf(route));
    setValue("");
    setHub("");
    setTyped(false);
    onDone();
  };
  return html`<form class="rail-add" onSubmit=${submit}>
    <input class="input mono" placeholder="192.168.10.101[:6900]" aria-label="Board address"
      value=${value} onInput=${(e) => setValue(e.target.value)} autofocus />
    <button type="submit" class="btn sm">Add</button>
    <input class="input mono via" placeholder="through a hub: hub, or an ssh host (optional)"
      aria-label="Through a hub: hub, or an ssh host" data-testid="add-via" value=${route}
      onInput=${(e) => { setHub(e.target.value); setTyped(true); }} />
    ${match ? html`<div class="rail-add-note" data-testid="add-route" role="note">
      <${Icon} name="info" cls="sm" /><span>boards.toml <b>${match.conf.key}</b>: ${routeText(match.conf)}${match.conf.target ? ` (${match.conf.target})` : ""}${typed && route && route !== viaDefault(match.conf) ? "; the route typed here is used instead" : ""}</span></div>` : null}
  </form>`;
}
