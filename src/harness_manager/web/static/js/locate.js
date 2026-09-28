// Identify from the sidebar and the Board tile (lane LOCATE; docs/design/BOARD_LOCATE.md).
//
// One small icon button per board, <LocateButton bid where="rail"|"tile">. A click asks the
// board to blink its panel for 5 s (POST /boards/{bid}/identify {seconds: 5}; the Linux
// harness's `locate`: the backlight at 2 Hz, and an "IDENTIFY: <who>" banner while the harness
// owns the panel) and counts down on the button from the answer's until_ms. It is:
// - enabled only when the board reports the harness feature "locate" (the identity HM already
//   read: the probe's, or the open board's). Otherwise it is aria-disabled, and the tooltip
//   says why. Nothing is asked of the board to decide;
// - explicit only: no poll, event or page load ever sends it, and it never rides bgOpts
//   (QUIET-POLL);
// - one at a time: a second press while it is on its way or blinking is IGNORED (it neither
//   extends nor restarts the blink). Stop (the small square beside the count) sends seconds 0;
// - allowed without the hub lease. The daemon lets one start go per board every 10 s
//   (409 ALREADY, with retry_after_s): the button then says when it can go again.
// A tap on the panel stops the blink on the board; this image has no panel read (R1/R2) to
// tell Harness Manager, so the count runs to its end regardless.
//
// The sidebar's hook (SIDEBAR-UX): app.js BoardItem renders <LocateButton bid where="rail" />
// as the <li>'s second child, beside (not inside) the card's own <button>.

import { call } from "./api.js";
import { html } from "./lib.js";
import { changed, log, onBoardEvent, S, timed } from "./store.js";
import { Icon, Spinner } from "./ui.js";

export const LOCATE_SECONDS = 5;
export const LOCATE_FEATURE = "locate";
// PANEL-TRUTH: harness_manager_mps3.capabilities.NEEDS_LOCATE, word for word: by feature,
// never a harness type guessed from a missing one.
export const NEEDS_LOCATE = "Identify isn't available on this harness image yet (harness feature 'locate')";
export const NOT_READ = "Not read yet: open the board (or scan again) to see whether it can blink";

// Per-board state, kept here and not in boardState(): a board in the sidebar that is not open
// must not gain a workspace record just because its button rendered.
const LS = {};

export function locateState(bid) {
  if (!LS[bid]) {
    LS[bid] = {
      sending: false,      // a start or a stop is on its way
      until: 0,            // ms: the countdown runs until then (0: not blinking)
      waitUntil: 0,        // ms: the daemon's 10 s limit said "not before then"
      error: null,         // the last failure (ApiError)
      line: "",            // "$ identify ... (rc, s)"
      note: "",            // the answer's note (someone else's lease)
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
  // No features at all: not read yet, unless the open board was read (an older harness).
  if (!feats.length && !(b && b.info)) return NOT_READ;
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

async function send(bid, seconds) {
  return timed(`identify ${bid} --seconds ${seconds}`,
    () => call("identify", { bid }, { seconds }));
}

// The click. Returns at once when it may not go: disabled, on its way, blinking (a second
// press is ignored), or inside the daemon's 10 s window.
export async function locate(bid) {
  const st = locateState(bid);
  if (st.sending || blinking(bid) || st.waitUntil > Date.now() || locateWhy(bid)) return;
  st.sending = true;
  st.error = null;
  st.note = "";
  changed();
  const r = await send(bid, LOCATE_SECONDS);
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
    // The countdown is the board's own "how long" (until_ms), not this host's clock.
    const ms = d.until_ms !== undefined ? Number(d.until_ms) : Number(d.seconds || LOCATE_SECONDS) * 1000;
    st.until = Date.now() + Math.max(0, ms);
    st.waitUntil = d.next_at ? Math.max(Date.now(), Number(d.next_at) * 1000) : 0;
    st.note = d.note || "";
    log("info", "identify", `${r.line}  blinking for ${Math.round(ms / 1000)} s${d.note ? ` (${d.note})` : ""}`, bid);
    tick();
  }
  changed();
}

// Stop: seconds 0 (never rate-limited). Only while it blinks.
export async function stopLocate(bid) {
  const st = locateState(bid);
  if (st.sending || !blinking(bid)) return;
  st.sending = true;
  changed();
  const r = await send(bid, 0);
  st.sending = false;
  st.line = r.line;
  if (r.error) {
    st.error = r.error;
    log("error", "identify", `${r.line}  ${r.error.errName}: ${r.error.message}`, bid);
  } else {
    st.until = 0;
    log("info", "identify", `${r.line}  stopped`, bid);
  }
  changed();
}

// An Identify from elsewhere (the CLI, another tab, Details) shows here too; nothing is sent.
onBoardEvent((ev) => {
  if (ev.topic !== "panel.locate" || !ev.board_id) return;
  const st = locateState(ev.board_id);
  const d = ev.data || {};
  if (d.state === "on" && !st.sending && !(st.until > Date.now())) {
    const secs = Number(d.seconds) || 0;
    if (secs > 0) { st.until = Date.now() + secs * 1000; tick(); }
  } else if (d.state === "off" && !st.sending) {
    st.until = 0;
  }
  changed();
});

function title(why, st) {
  if (why) return why;
  if (st.sending) return "Asking the board...";
  if (st.until > Date.now()) return `Blinking: ${left(st.until)} s left (another press does nothing; the square stops it)`;
  if (st.waitUntil > Date.now()) {
    return `Identified just now: once every 10 s per board. Again in ${left(st.waitUntil)} s`;
  }
  return `Identify: blink this board's panel for ${LOCATE_SECONDS} s`;
}

export function LocateButton({ bid, where = "rail" }) {
  const st = locateState(bid);
  const why = locateWhy(bid);
  const now = Date.now();
  const on = st.until > now;
  const wait = !on && st.waitUntil > now;
  const state = why ? "disabled" : st.sending ? "sending" : on ? "blinking" : wait ? "wait" : "idle";
  const off = state !== "idle";
  const label = on ? `Identify: blinking, ${left(st.until)} seconds left` : "Identify this board";
  return html`<span class=${`locate locate-${where}`} data-testid=${`${where}-locate-wrap`}>
    <button type="button" class=${`btn ghost sm icon-only locate-btn ${state}`}
      data-testid=${`${where}-locate`} data-action="locate" data-board=${bid} data-state=${state}
      aria-disabled=${off ? "true" : undefined} aria-label=${label} title=${title(why, st)}
      onClick=${(e) => { e.stopPropagation(); if (!off) locate(bid); }}>
      ${st.sending ? html`<${Spinner} />`
        : on ? html`<span class="locate-count" data-testid=${`${where}-locate-count`}>${left(st.until)}</span>`
          : html`<${Icon} name="scan-search" />`}
    </button>
    ${on ? html`<button type="button" class="btn ghost sm icon-only locate-stop"
        data-testid=${`${where}-locate-stop`} data-action="locate-stop" data-board=${bid}
        aria-label="Stop blinking" title="Stop blinking"
        onClick=${(e) => { e.stopPropagation(); stopLocate(bid); }}><${Icon} name="square" /></button>` : null}
    ${where === "tile" ? html`<${TileLine} st=${st} why=${why} wait=${wait} />` : null}
  </span>`;
}

// The Board tile has room for words.
function TileLine({ st, why, wait }) {
  const on = st.until > Date.now();
  let text = "";
  let level = "muted";
  if (why) { text = why; level = "unk"; }
  else if (on) { text = `blinking, ${left(st.until)} s left`; level = "busy"; }
  else if (wait) { text = `identified: again in ${left(st.waitUntil)} s`; }
  else if (st.error) { text = `${st.error.errName}: ${st.error.message}`; level = "err"; }
  else { text = `Identify: blink the panel for ${LOCATE_SECONDS} s`; }
  return html`<span class=${`locate-line pl ${level}`} data-testid="tile-locate-line">${text}</span>
    ${st.note ? html`<span class="locate-note muted small" data-testid="tile-locate-note">${st.note}</span>` : null}`;
}
