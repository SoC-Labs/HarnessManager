// Identify from the sidebar and the Board tile (lane LOCATE; docs/design/BOARD_LOCATE.md).
//
// One small icon button per board, <LocateButton bid where="rail"|"tile">. A click asks the
// board to blink its user LEDs and panel for 5 s (POST /boards/{bid}/identify {seconds: 5})
// and counts 5-4-3-2-1 on the button. It is:
// - enabled only when the board reports the harness feature "locate" (the identity HM already
//   read: the probe's, or the open board's). Otherwise it is aria-disabled, and the tooltip
//   says why. Nothing is asked of the board to decide;
// - explicit only: no poll, event or page load ever sends it, and it never rides bgOpts
//   (QUIET-POLL). A second press while one is on its way or running sends nothing;
// - allowed without the hub lease. The daemon lets one start go per board every 10 s
//   (409 ALREADY, with retry_after_s): the button then says when it can go again;
// - told when someone else identified the board (a panel.locate event from the board's own
//   ring, source "board", mine false): "Identified by bob@lab-pc-03 at 14:02:05".
//
// The sidebar's hook (SIDEBAR-UX): app.js BoardItem renders <LocateButton bid where="rail" />
// as the <li>'s second child, beside (not inside) the card's own <button>.

import { call } from "./api.js";
import { clock } from "./format.js";
import { html } from "./lib.js";
import { changed, log, onBoardEvent, S, timed } from "./store.js";
import { Icon, Spinner } from "./ui.js";

export const LOCATE_SECONDS = 5;
export const LOCATE_FEATURE = "locate";
export const NEEDS_LOCATE = "needs harness feature 'locate' (Linux harness)";
export const NOT_READ = "Not read yet: open the board (or scan again) to see whether it can blink";

// Per-board state, kept here and not in boardState(): a board in the sidebar that is not open
// must not gain a workspace record just because its button rendered.
const LS = {};

export function locateState(bid) {
  if (!LS[bid]) {
    LS[bid] = {
      sending: false,      // the POST is on its way
      until: 0,            // ms: the countdown runs until then (0: not blinking)
      waitUntil: 0,        // ms: the daemon's 10 s limit said "not before then"
      error: null,         // the last failure (ApiError)
      line: "",            // "$ identify ... (rc, s)"
      note: "",            // the answer's note (another person's lease)
      by: null,            // {who, at}: someone else identified this board
    };
  }
  return LS[bid];
}

function identityOf(bid) {
  const row = S.boards[bid] || {};
  const b = S.board[bid];
  return (b && b.info && b.info.identity) || (row.candidate && row.candidate.identity) || null;
}

// "" when the board can blink, else the tooltip's reason. Only what HM already knows.
export function locateWhy(bid) {
  const b = S.board[bid];
  const unavailable = b && b.info && b.info.unavailable;
  const ident = identityOf(bid);
  const feats = ident && Array.isArray(ident.features) ? ident.features : [];
  if (feats.includes(LOCATE_FEATURE)) return "";
  if (unavailable && unavailable.locate) return `Cannot: ${unavailable.locate}`;
  if (!feats.length) return NOT_READ;
  return `Cannot: ${NEEDS_LOCATE}`;
}

function left(ms) {
  return Math.max(0, Math.ceil((ms - Date.now()) / 1000));
}

export function blinking(bid) {
  return locateState(bid).until > Date.now();
}

let ticker = null;

function tick() {
  if (ticker) return;
  ticker = setInterval(() => {
    const now = Date.now();
    const live = Object.values(LS).some((st) => st.until > now || st.waitUntil > now);
    changed();
    if (!live) { clearInterval(ticker); ticker = null; }
  }, 250);
}

// The click. Returns at once when it may not go (disabled, on its way, blinking, waiting).
export async function locate(bid) {
  const st = locateState(bid);
  if (st.sending || blinking(bid) || st.waitUntil > Date.now() || locateWhy(bid)) return;
  st.sending = true;
  st.error = null;
  st.note = "";
  changed();
  const r = await timed(`identify ${bid} --seconds ${LOCATE_SECONDS}`,
    () => call("identify", { bid }, { seconds: LOCATE_SECONDS }));
  st.sending = false;
  st.line = r.line;
  if (r.error) {
    st.error = r.error;
    const d = r.error.data || {};
    if (r.error.errName === "ALREADY" && d.retry_after_s !== undefined) {
      st.waitUntil = Date.now() + Number(d.retry_after_s) * 1000;
      tick();
    }
    log("error", "identify", `${r.line}  ${r.error.errName}: ${r.error.message}`, bid);
  } else {
    const d = r.data.data || {};
    // The countdown is the 5 s asked for, from the answer (not the daemon's clock).
    st.until = Date.now() + Number(d.seconds || LOCATE_SECONDS) * 1000;
    st.waitUntil = d.next_at ? Math.max(Date.now(), Number(d.next_at) * 1000) : 0;
    st.note = d.note || "";
    log("info", "identify", `${r.line}  blinking for ${d.seconds} s${d.note ? ` (${d.note})` : ""}`, bid);
    tick();
  }
  changed();
}

// Someone started an Identify: this page (the daemon's own event) or another Harness Manager
// (the board's ring, source "board"). Neither sends anything.
onBoardEvent((ev) => {
  if (ev.topic !== "panel.locate" || !ev.board_id) return;
  const st = locateState(ev.board_id);
  const d = ev.data || {};
  if (d.source === "board") {
    if (!d.mine) st.by = { who: d.who || "someone", at: Number(d.at) || Number(ev.at) || Date.now() / 1000 };
  } else if (d.state === "on" && !st.sending && !(st.until > Date.now())) {
    const secs = Number(d.seconds) || 0;       // an Identify from the CLI or another tab
    if (secs > 0) { st.until = Date.now() + secs * 1000; tick(); }
  } else if (d.state === "off") {
    st.until = 0;
  }
  changed();
});

function title(why, st) {
  if (why) return why;
  if (st.sending) return "Asking the board...";
  if (st.until > Date.now()) return `Blinking: ${left(st.until)} s left`;
  if (st.waitUntil > Date.now()) {
    return `Identified just now: once every 10 s per board. Again in ${left(st.waitUntil)} s`;
  }
  const by = st.by ? ` (last identified by ${st.by.who} at ${clock(st.by.at)})` : "";
  return `Identify: blink this board's LEDs and panel for ${LOCATE_SECONDS} s${by}`;
}

export function LocateButton({ bid, where = "rail" }) {
  const st = locateState(bid);
  const why = locateWhy(bid);
  const now = Date.now();
  const on = st.until > now;
  const wait = !on && st.waitUntil > now;
  const state = why ? "disabled" : st.sending ? "sending" : on ? "blinking" : wait ? "wait" : "idle";
  const off = state !== "idle";
  const tip = title(why, st);
  const label = on ? `Identify: blinking, ${left(st.until)} seconds left` : "Identify this board";
  return html`<span class=${`locate locate-${where}`} data-testid=${`${where}-locate-wrap`}>
    <button type="button" class=${`btn ghost sm icon-only locate-btn ${state}`}
      data-testid=${`${where}-locate`} data-action="locate" data-board=${bid} data-state=${state}
      aria-disabled=${off ? "true" : undefined} aria-label=${label} title=${tip}
      onClick=${(e) => { e.stopPropagation(); if (!off) locate(bid); }}>
      ${st.sending ? html`<${Spinner} />`
        : on ? html`<span class="locate-count" data-testid=${`${where}-locate-count`}>${left(st.until)}</span>`
          : html`<${Icon} name="scan-search" />`}
    </button>
    ${where === "tile" ? html`<${TileLine} bid=${bid} st=${st} why=${why} wait=${wait} />` : null}
  </span>`;
}

// The Board tile has room for words: what happened, and who else identified the board.
function TileLine({ bid, st, why, wait }) {
  const on = st.until > Date.now();
  let text = "";
  let level = "muted";
  if (why) { text = why; level = "unk"; }
  else if (on) { text = `blinking, ${left(st.until)} s left`; level = "busy"; }
  else if (wait) { text = `identified: again in ${left(st.waitUntil)} s`; }
  else if (st.error) { text = `${st.error.errName}: ${st.error.message}`; level = "err"; }
  else { text = `Identify: blink LEDs and panel for ${LOCATE_SECONDS} s`; }
  return html`<span class=${`locate-line pl ${level}`} data-testid="tile-locate-line">${text}</span>
    ${st.note ? html`<span class="locate-note muted small" data-testid="tile-locate-note">${st.note}</span>` : null}
    ${st.by ? html`<span class="locate-by" data-testid="tile-locate-by"><${Icon} name="scan-search" cls="sm" />Identified by ${st.by.who} at ${clock(st.by.at)}</span>` : null}`;
}
