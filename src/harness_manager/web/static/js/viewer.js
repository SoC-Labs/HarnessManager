// QUIET-POLL: this page tells harness-manager-daemon which board it is showing.
//
// A lab board's control port serves ONE client, so the daemon contacts a board in the
// background (presence beats, the reads nobody clicked) only while a page actually shows it
// (docs/API.md "Background reads", services/quiet.py). This page is "viewing" the selected
// board while the page is visible: it PUTs a viewer for it, refreshes it every 20 s, and
// DELETEs it when the user looks at another board, hides the page or closes it. A page that
// stops refreshing (a sleeping laptop) lapses on its own after the viewer's ttl.
//
// Every read this page makes on its own (a timer, an event, the first read of a board) is
// marked background (`call(..., { background: true })`): the daemon answers it with
// `quiet` instead of touching the board when the gate says no. A click is never marked.

import { call, routeMissing } from "./api.js";

const HEARTBEAT_MS = 20000;
const TTL_S = 45;

function newId() {
  try {
    const saved = window.sessionStorage.getItem("harness_manager.viewer");
    if (saved) return saved;
  } catch (e) { /* not remembered */ }
  const bytes = new Uint8Array(8);
  if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  const id = `page-${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
  try { window.sessionStorage.setItem("harness_manager.viewer", id); } catch (e) { /* ok */ }
  return id;
}

export const VIEWER_ID = newId();

let current = null;          // the board this page has a live viewer on
let wanted = null;           // the board it should be viewing (selected, open, page visible)
let unsupported = false;     // an older daemon (or the T14 mock): no viewer routes
let timer = 0;
let onState = () => {};      // (bid, background) -> void: the store keeps the gate's state

export function onBackgroundState(fn) { onState = fn; }

// The board this page shows now, or null. The store's background reads add X-HM-Viewer
// for it, so the first read after a selection is never refused for want of a viewer.
export function viewing() { return document.visibilityState === "visible" ? wanted : null; }

async function put(bid) {
  if (unsupported || !bid) return;
  try {
    const { data } = await call("viewerPut", { bid, vid: VIEWER_ID }, { ttl_s: TTL_S });
    current = bid;
    if (data && data.background) onState(bid, data.background);
  } catch (e) {
    if (routeMissing(e)) unsupported = true;
  }
}

function drop(bid, { beacon = false } = {}) {
  if (unsupported || !bid) return;
  if (current === bid) current = null;
  if (beacon) {
    // The page is going away: a keepalive request outlives it (sendBeacon cannot DELETE).
    call("viewerDelete", { bid, vid: VIEWER_ID }, undefined, null, { keepalive: true })
      .catch(() => {});
    return;
  }
  call("viewerDelete", { bid, vid: VIEWER_ID }).catch(() => {});
}

function sync() {
  const want = viewing();
  if (current && current !== want) drop(current);
  clearInterval(timer);
  timer = 0;
  if (!want) return;
  put(want);
  timer = setInterval(() => { if (viewing()) put(viewing()); }, HEARTBEAT_MS);
}

// The gate's state again now (after a read the user asked for: the lease may have moved).
export function refreshState(bid) {
  if (!unsupported && bid && viewing() === bid) put(bid);
}

// The store calls this whenever the selection or a board's open state changes.
export function setViewing(bid) {
  if (bid === wanted) return;
  wanted = bid || null;
  sync();
}

document.addEventListener("visibilitychange", sync);
window.addEventListener("pagehide", () => { if (current) drop(current, { beacon: true }); });
