// Lease requests, force release and leaving the queue (docs/LEASE_REQUESTS.md, lane LR-D).
//
// Three people see this, from any section of the page (it renders above the header):
// - the REQUESTER: "Request board" (a small form, an optional message), then a bar with
//   the queue position, "the holder has been asked", the holder's 2:00 countdown, Leave
//   queue, and "Force release..." once the daemon says force is available. Force opens a
//   red confirm naming the holder; it is the only way to force. When no Harness Manager
//   session is known to hold the board (lease.holder_kind is not "hm": it never answered,
//   so it may be a script, a soak or runner; D12), the confirm asks for the board's name
//   typed, and Force stays disabled until it matches.
// - the HOLDER: a prompt per incoming request: who wants the board and why, the
//   countdown to their force, Release now, or Keep for 5 / 15 / 30 / 60 min (+ message).
// - the VICTIM: a banner "<board> was force-released by <by> at <time>: <reason>" that
//   stays until dismissed (across reloads) and is written to Activity.
//
// LEASE-UI adds who holds it in words (the rail's badge: Yours, Held by X, Free, Queued), a
// Release that always asks first, and Close board asking whether to release a lease this
// Harness Manager holds. "Yours" is `lease.here`, never `mine` (leaseWho, week.js).
//
// Every countdown runs to a time the daemon read from the hub's note (deadline_at, an
// answer's at + minutes), never to a timer this page started. docs/LEASE_REQUESTS.md D1-D8
// amend the frozen API: a keep answer is a phase of the request job (D1), leaving ends it
// with {left: true} (D7), GET /lease names the physical board (D4) and each incoming
// request's answer (D5). Over fpgahub's REST API (T8, docs/HUB_MODE.md) there are no
// messages and no Keep (notes_supported false), and a non-admin token cannot force.

import { gateReason, interlock, panelState, runAction, runJob } from "./actions.js";
import { call, routeMissing } from "./api.js";
import { boardName, clock, deployBar, hostOf } from "./format.js";
import { leaseSpecs } from "./hub.js";
import { html, useLayoutEffect, useRef, useState } from "./lib.js";
import { changed, log, onBoardEvent, S, timed } from "./store.js";
import {
  durationText, epochOf, leaseLeft, leaseName, leaseTargetNote, leaseWhere, leaseWho, loadHub,
  onHubLoaded, scheduleHub, staleNote, week,
} from "./week.js";
import { Chip, Icon, MiniBar, Reason, ResultBlock, Seg, Spinner } from "./ui.js";

export const KEEP_MINUTES = [5, 15, 30, 60];
const NOTE_MAX = 500;                   // characters; a note on the hub is at most 4 KiB

// --- small helpers -----------------------------------------------------------------------------

function hubOf(bid) {
  const b = S.board[bid];
  return (b && b.week && b.week.hub) || null;
}

// T8 hub mode over REST: request notes (the message, Keep) and the right to force.
export function notesOff(bid) {
  const hub = hubOf(bid);
  if (hub) return hub.notesOk === false ? (hub.notesReason || "this hub connection carries no request messages") : "";
  const d = FULL[bid] && FULL[bid].data;             // a board that is not open (G3)
  return d && d.notes_supported === false ? (d.notes_reason || "this hub connection carries no request messages") : "";
}

function revokeOff(bid) {
  const hub = hubOf(bid);
  return hub && hub.revokeOk === false ? (hub.revokeReason || "force-release needs an admin token on this hub") : "";
}

// What every lease text calls the board: its name (N1: "mps3-01", from boards.toml, the
// harness or the hub), else its address.
export function leaseBoardName(bid) {
  const b = S.board[bid];
  const row = S.boards[bid] || {};
  return boardName((b && b.info && b.info.candidate) || row.candidate || null, bid);
}

// TARGET in the `$ lease ...` lines: the board's address, as every CLI verb takes it.
function cliTarget(bid) { return hostOf(bid); }

// D12: the holder may be a script (no Harness Manager session answered for it). The daemon's
// lease.holder_kind says; a daemon without it counts as "maybe a script".
export function holderMayBeScript(bid) {
  const hub = hubOf(bid);
  return !!(hub && hub.lease && !hub.lease.mine && hub.lease.holder_kind !== "hm");
}

// What the force confirm asks to be typed: the board's name (N1), else the hub's board as
// people write it (mps3_01 -> mps3-01), else its address. The daemon accepts it (D12).
export function forceName(bid) {
  const b = S.board[bid];
  const row = S.boards[bid] || {};
  const cand = (b && b.info && b.info.candidate) || row.candidate || null;
  const hub = hubOf(bid);
  return (cand && cand.name) || (hub && hub.board ? String(hub.board).replace(/_/g, "-") : "") || hostOf(bid);
}

export function nameMatches(typed, name) {
  return String(typed || "").trim().toLowerCase() === String(name || "").trim().toLowerCase();
}

export function secondsTo(at, now = Date.now() / 1000) {
  const e = epochOf(at);
  return e === null ? null : e - now;
}

// 95 -> "1:35"; never negative.
export function mmss(s) {
  const t = Math.max(0, Math.ceil(s));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
}

function minutesLeft(s) { return Math.max(1, Math.ceil(s / 60)); }

function quoted(text) { return `“${text}”`; }

function shellQuote(text) { return `'${String(text).replace(/'/g, "'\\''")}'`; }

// When a keep answer runs out: its `at` plus its minutes.
function keepUntil(answer) {
  if (!answer || answer.answer !== "keep" || !answer.minutes) return null;
  const at = epochOf(answer.at);
  return at === null ? null : at + 60 * Number(answer.minutes);
}

function remember(store, key, value) {
  try { store.setItem(key, JSON.stringify(value)); } catch (e) { /* this page only */ }
}

function recall(store, key) {
  try { return JSON.parse(store.getItem(key) || "{}") || {}; } catch (e) { return {}; }
}

function localStore() { try { return window.localStorage; } catch (e) { return null; } }

// --- page state --------------------------------------------------------------------------------

const DISMISSED_KEY = "harness_manager.lease_taken_dismissed";   // {bid: at}, survives reloads

const L = {
  form: null,                // {bid, message, trigger}: the "Request board" form
  confirm: null,             // {bid, trigger}: the force confirm
  leaving: new Set(),        // boards whose request this page withdrew
  // What this page answered, until GET /lease carries it (D5: incoming[].answer) or the
  // request is gone (a release): the fallback for a daemon without D5, this page only.
  answers: {},
  dismissedTaken: recall(localStore(), DISMISSED_KEY),
  takenLogged: new Set(),
  resultDismissed: {},       // bid -> the startedAt of the request result the user closed
  answerDismissed: new Set(),
  zeroSeen: new Set(),       // request ids whose countdown this page saw reach zero
  seenReq: {},               // bid -> {id, deadline_at}: the request as last read
  reasked: {},               // bid -> {holder, deadline_at}: D9, a new holder was asked
  release: null,             // {bid, trigger}: LEASE-UI, the Release confirm
  closing: null,             // {bid, trigger, doClose, busy, error, choice}: UI v2, the Close dialog
  pop: null,                 // bid: UI v2, the header's queue popover is open for it
};

// --- UI v2 (SHELL-2): the hub's full queue, a lease read without opening, "N waiting" -----------
//
// GET /boards/{bid}/lease (docs/API.md "UI v2: hub leases", G3 + G11) answers for a board that is
// not open too, and lists every waiter: `queue` (people, the interactive tier, in the hub's
// order, each {position, holder, user, mine, tier, request_id, message, want_s, since}) and
// `background_queue` (automation, which waits behind every person). An open board's read is
// week.js's (w.hub, every 30 s); FULL holds the reads this page makes itself: the preview of a
// board that is not open, and the popover when it opens (the background tier, fresh).

const FULL = {};             // bid -> {data, error, at, loading}
// The identity this page last read of a board it closed (and the baseline after "Restore
// baseline and close"): the preview's Loaded row, newer than the probe's snapshot.
const LAST_SEEN = {};        // bid -> {identity, at}

export function lastSeen(bid) { return LAST_SEEN[bid] || null; }

export function fullLease(bid) { return FULL[bid] || null; }

export async function readLease(bid) {
  const f = FULL[bid] || (FULL[bid] = { data: null, error: null, at: 0, loading: false });
  if (f.loading) return f;
  f.loading = true;
  const r = await timed(`lease show ${cliTarget(bid)}`, () => call("lease", { bid }));
  f.loading = false;
  f.at = Date.now() / 1000;
  if (r.error) f.error = r.error;
  else { f.data = r.data.data; f.error = null; }
  changed();
  return f;
}

// "30 min", "1 h", "1.5 h": how long a waiter wants the board (want_s; 0 = not said).
export function wantText(s) {
  const n = Number(s || 0);
  if (!n) return "";
  if (n < 3600) return `${Math.round(n / 60)} min`;
  const h = n / 3600;
  return `${Number.isInteger(h) ? h : h.toFixed(1)} h`;
}

// "1 h 10 min", "47 min", "40 s": time left on a lease.
export function leftText(s) {
  if (s === null || s === undefined) return "";
  if (s < 90) return `${Math.max(0, Math.round(s))} s`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}`;
}

function hhmmOf(at) {
  const e = epochOf(at);
  return e === null ? "" : clock(e).slice(0, 5);
}

// One waiter as the lists show it. `you`: the request THIS Harness Manager sent (its id, or its
// place); the same hub name without it is another session of yours, named as such.
function waiter(x, tier, req) {
  const you = !!(req && ((x.request_id && x.request_id === req.id)
    || (!x.request_id && x.mine && req.position && x.position === req.position)));
  return {
    pos: x.position || 0, who: x.holder || x.user || "someone", user: x.user || "",
    tier: x.tier || tier, you, mine: !!x.mine, want: wantText(x.want_s),
    since: hhmmOf(x.since), msg: x.message || "",
  };
}

// The queue of a board as this page last read it: {people, bots, botsKnown, botsReason,
// request, at}, or null when nothing was read. An open board's comes from week.js (the
// background tier from this page's own read when week.js does not keep it).
export function queueOf(bid) {
  const row = S.boards[bid] || {};
  const hub = row.open ? hubOf(bid) : null;
  const f = FULL[bid] && FULL[bid].data;
  const src = hub || (!row.open ? f : null);
  if (!src) return null;
  const req = src.request || null;
  const people = (Array.isArray(src.queue) ? src.queue : []).map((x) => waiter(x, "interactive", req));
  let bg = null;
  if (Array.isArray(src.background_queue)) bg = src;
  else if (f && Array.isArray(f.background_queue)) bg = f;
  const bots = bg ? bg.background_queue.map((x) => waiter(x, "background", null)) : [];
  return {
    people, bots, request: req,
    botsKnown: !!bg && bg.background_known !== false,
    botsReason: bg ? bg.background_reason || "" : "not read yet",
  };
}

// Every waiter in one line each: the chip's and the rail's tooltip.
export function queueTitle(q) {
  if (!q) return "";
  return [...q.people, ...q.bots].map((x) => `${x.tier === "background" ? "automation " : ""}#${x.pos} `
    + `${x.you ? "you" : x.who}${x.want ? ` · wants ${x.want}` : ""}${x.since ? ` · since ${x.since}` : ""}`
    + `${x.msg ? `: “${x.msg}”` : ""}`).join("\n");
}

export function queueCount(q) { return q ? q.people.length + q.bots.length : 0; }

// The lease of a board that is not open, from this page's own read (G3), else what the service
// last knew (GET /boards lease_known, no hub call), in leaseWho's words: here, elsewhere,
// other, free, unknown, unread (behind a hub, nothing read) or none (no hub).
export function previewWho(bid) {
  const row = S.boards[bid] || {};
  const f = FULL[bid];
  const d = f && f.data;
  if (d) {
    if (!d.hub && !d.lease) return { state: "none" };
    const lease = d.lease || null;
    const base = { lease, host: d.hub || "", target: (lease && lease.target) || "",
      board: (lease && lease.board) || d.board || "", request: d.request || null,
      position: (d.request && d.request.position) || null, source: "read", at: f.at };
    if (!lease) return { ...base, state: "free", holder: "" };
    if (lease.here === undefined ? lease.mine : lease.here) return { ...base, state: "here", holder: lease.holder || "" };
    if (lease.mine) return { ...base, state: "elsewhere", holder: lease.holder || "your hub name" };
    return { ...base, state: "other", holder: lease.holder || "someone else" };
  }
  const k = row.lease_known;
  if (k) {
    const lease = k.state === "free" ? null : { holder: k.holder || "", expires_at: k.expires_at || "",
      mine: !!k.mine, here: !!k.here, target: k.target || "", board: k.board || "" };
    const base = { lease, host: k.hub || "", target: k.target || "", board: k.board || "", request: null,
      position: null, source: "known", at: epochOf(k.confirmed_at),
      error: f && f.error ? `${f.error.errName}: ${f.error.message}` : "" };
    if (!lease) return { ...base, state: "free", holder: "" };
    if (k.here) return { ...base, state: "here", holder: k.holder || "" };
    if (k.mine) return { ...base, state: "elsewhere", holder: k.holder || "your hub name" };
    return { ...base, state: "other", holder: k.holder || "someone else" };
  }
  if (f && f.error) return { state: "unknown", holder: "", error: `${f.error.errName}: ${f.error.message}` };
  return { state: "unread" };
}

// leaseWho for an open board, previewWho for one that is not.
export function leaseView(bid) {
  const row = S.boards[bid] || {};
  return row.open ? leaseWho(bid) : previewWho(bid);
}

// The queue, people first, then automation apart (it waits behind every person).
export function LeaseQueueList({ q, compact = false, testid = "lease-queue" }) {
  const row = (x) => html`<li key=${`${x.tier}${x.pos}`} class=${`lq-row ${x.you ? "you" : ""}`}
      data-tier=${x.tier} data-you=${x.you ? "true" : undefined} title=${x.msg ? `“${x.msg}”` : undefined}>
    <span class="lq-pos">#${x.pos}</span>
    <span class="lq-body"><span class="lq-who"><b>${x.you ? "You" : x.who}</b>${x.mine && !x.you
      ? html`<span class="muted small">another session of yours</span>` : null}</span>
      <span class="lq-meta">${[x.want ? `wants ${x.want}` : "", x.since ? `since ${x.since}` : ""].filter(Boolean).join(" · ")
        || (x.tier === "background" ? "automation" : "no note: how long and why are not known")}</span>
      ${x.msg && !compact ? html`<span class="lq-msg">“${x.msg}”</span>` : null}</span></li>`;
  return html`<div class="lq-list" data-testid=${testid}>
    ${q.people.length ? html`<ol class="lq-rows">${q.people.map(row)}</ol>`
      : html`<div class="lq-none">No interactive requests</div>`}
    ${q.bots.length ? html`<div class="lq-bg-head" title="Automation (e.g. Checks) waits in the hub's background queue, behind every interactive request">
        <${Icon} name="repeat" cls="sm" />Automation · background queue</div>
      <ol class="lq-rows bg" data-testid=${`${testid}-bg`}>${q.bots.map(row)}</ol>`
      : !q.botsKnown && q.botsReason && !compact ? html`<div class="lq-none" data-testid=${`${testid}-bg-unknown`}>
        Automation: not known (${q.botsReason})</div>` : null}
  </div>`;
}

// The header's "N waiting" (plan S32, round 3): every waiter the hub reports on this board.
// Amber when the lease is yours (someone waits for you). It opens the popover.
export function QueueChip({ bid }) {
  const q = queueOf(bid);
  const n = queueCount(q);
  if (!n) return null;
  const open = L.pop === bid;
  const yours = leaseWho(bid).state === "here";
  return html`<button type="button" class=${`chip lq-chip ${yours ? "warn" : "plain"}`} data-testid="lease-queue-chip"
    aria-haspopup="dialog" aria-expanded=${open ? "true" : "false"} title=${queueTitle(q)}
    data-action="lease_queue" onClick=${() => { if (open) closePop(); else openPop(bid); }}>
    <${Icon} name="user" />${n} waiting</button>`;
}

function openPop(bid) {
  L.pop = bid;
  changed();
  readLease(bid);                      // the background tier and the notes, fresh
}

function closePop(focus = true) {
  const was = L.pop;
  L.pop = null;
  changed();
  if (focus && was) {
    requestAnimationFrame(() => {
      const chip = document.querySelector('[data-testid="lease-queue-chip"]');
      if (chip) chip.focus();
    });
  }
}

// The hub's queue, under the chip: who holds it, every waiter (position, who, how long, since,
// the message), automation apart, and the one action that fits. Escape or a click outside
// closes it.
export function QueuePop({ bid }) {
  return L.pop === bid ? html`<${QueuePopOpen} bid=${bid} />` : null;
}

function QueuePopOpen({ bid }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && L.pop) { e.stopPropagation(); closePop(); } };
    const onDown = (e) => { if (!e.target.closest(".lq-pop, .lq-chip")) closePop(false); };
    window.addEventListener("keydown", onKey, true);
    document.addEventListener("mousedown", onDown);
    return () => { window.removeEventListener("keydown", onKey, true); document.removeEventListener("mousedown", onDown); };
  }, []);
  const q = queueOf(bid) || { people: [], bots: [], botsKnown: false, botsReason: "" };
  const who = leaseWho(bid);
  const left = leaseLeft(who.lease);
  const holder = who.state === "here" ? `You hold it${left !== null ? ` · ${leftText(left)} left` : ""}`
    : who.state === "other" ? `${who.holder} holds it${left !== null ? ` · ${leftText(left)} left` : ""}`
    : who.state === "elsewhere" ? `${who.holder} holds it in another session, not this Harness Manager${left !== null ? ` · ${leftText(left)} left` : ""}`
    : who.state === "free" ? "Nobody holds it" : who.state === "unknown" ? "The lease could not be read" : "Reading the lease…";
  const next = q.people.find((x) => !x.you) || q.bots[0] || null;
  const mine = q.people.find((x) => x.you);
  const requesting = requestActive(bid);
  let foot = null;
  if (who.state === "here" && next) {
    foot = html`<span class="lq-foot-t">${next.tier === "background" ? `The automation run ${next.who}` : next.who.split("@")[0]} gets it when you release.</span>
      <button type="button" class="btn release sm" data-action="lease_release_open"
        onClick=${(e) => { closePop(false); openReleaseConfirm(bid, e.currentTarget); }}><${Icon} name="lock-open" /> Release…</button>`;
  } else if (who.state === "other" || who.state === "elsewhere") {
    foot = mine || requesting
      ? html`<span class="lq-foot-t">You are #${(mine && mine.pos) || who.position || "?"}: ${who.holder.split("@")[0]} sees your request.</span>
        <button type="button" class="btn sm" data-action="lease_leave_pop"
          onClick=${() => { closePop(false); leaveQueue(bid); }}><${Icon} name="x" /> Cancel request</button>`
      : who.state === "other" ? html`<span class="lq-foot-t">Requesting doesn't open or lock the board.</span>
        <button type="button" class="btn primary sm" data-action="lease_request_pop"
          onClick=${(e) => { closePop(false); openRequestForm(bid, e.currentTarget); }}><${Icon} name="send" /> Request…</button>`
      : html`<span class="lq-foot-t">Stop that session to hand the board on.</span>`;
  }
  const target = leaseName(who) || leaseBoardName(bid);
  return html`<div class="lq-pop" role="dialog" aria-label=${`Hub lease queue for ${leaseBoardName(bid)}`} ref=${ref}
      data-testid="lease-queue-pop">
    <div class="lq-head"><b>Hub lease queue</b><span class="muted mono">${target}</span>
      <button type="button" class="btn ghost icon-only sm" aria-label="Close the queue" title="Close (Esc)"
        onClick=${() => closePop()}><${Icon} name="x" /></button></div>
    <div class="lq-holder" data-testid="lease-queue-holder"><${Icon} name=${who.state === "here" ? "user" : "lock"} />${holder}</div>
    <${LeaseQueueList} q=${q} />
    ${notesOff(bid) ? html`<div class="lq-none">${`No messages or times over this hub connection: ${notesOff(bid)}.`}</div>` : null}
    ${foot ? html`<div class="lq-foot">${foot}</div>` : null}
  </div>`;
}

// --- the requester -------------------------------------------------------------------------------

export function requestActive(bid) {
  const hub = hubOf(bid);
  const b = S.board[bid];
  return !!((hub && hub.request) || (b && b.job && b.job.kind === "lease_request")
    || panelState(bid, "lease_req").running);
}

// How long you want the board (G11 want_s, in the request note): the Request dialog's choices.
export const WANT_CHOICES = [{ value: 1800, label: "30 min" }, { value: 3600, label: "1 h" }, { value: 7200, label: "2 h" }];
const WANT_DEFAULT = 3600;

export function openRequestForm(bid, trigger = null) {
  L.form = { bid, message: "", want: WANT_DEFAULT, trigger: trigger || document.activeElement };
  changed();
  if (!(S.boards[bid] && S.boards[bid].open)) readLease(bid);     // the queue you would join
}

function closeForm() {
  const t = L.form && L.form.trigger;
  L.form = null;
  changed();
  if (t && t.isConnected) setTimeout(() => t.focus(), 0);
}

const PHASE_TEXT = {
  queued: (d) => `queued${d.done ? ` (position ${d.done})` : ""}`,
  notified: () => "the holder has been asked",
  answered: () => "the holder answered: you stay in the queue",
  "force-available": () => "no answer in time: force release is available",
  held: () => "held",
};

// After a lease action: re-read what shows it (an open board's header, a closed one's preview).
function afterLease(bid) {
  if (S.boards[bid] && S.boards[bid].open) loadHub(bid);
  else readLease(bid);
}

// `want` (seconds, G11): how long you want the board; it rides the request note, so a hub
// connection without notes (REST) carries neither it nor the message. The CLI has no flag for
// it yet: the command line shows the message only.
function requestSpec(bid, message, want = 0) {
  const name = leaseBoardName(bid);
  const body = { ...(message ? { message } : {}), ...(want ? { want_s: want } : {}) };
  let seen = "";
  return {
    key: "lease_request", label: "Send request", busyLabel: "Waiting for the board...",
    budgetS: 7200,           // a queue may wait for hours: past this the panel only notes it
    command: `lease request ${cliTarget(bid)}${message ? ` --message ${shellQuote(message)}` : ""}`,
    run: async (ctx) => {
      const onProgress = (d) => {
        ctx.progress((PHASE_TEXT[d.phase] || (() => d.phase || "waiting"))(d), d.phase);
        // A board that is not open has no header read: its preview reads the lease per phase.
        if (!(S.boards[bid] && S.boards[bid].open) && d.phase !== seen) { seen = d.phase; readLease(bid); }
      };
      try {
        return await runJob("leaseRequest", { bid }, body, onProgress, "lease_request");
      } catch (e) {
        if (L.leaving.has(bid)) return { left: true };        // we withdrew it: not a failure
        if (!routeMissing(e)) throw e;
        // A daemon from before lease requests (LR-C): queue plainly, as "Queue for it" did.
        ctx.progress("this harness-manager-daemon has no lease requests: queued without asking the holder", "fallback");
        return await runJob("leaseTake", { bid }, {}, onProgress, "lease");
      } finally {
        L.leaving.delete(bid);
        const w = week(bid);
        w.leaseQueued = false;
      }
    },
    render: (r) => {
      if (r && r.left) return [{ kind: "out", text: "you left the queue: the request is withdrawn" }];
      if (r && r.answered) {
        const a = r.answered;
        return [{ kind: "warnline", text: `the holder answered: keep for ${a.minutes} min${a.message ? `: ${quoted(a.message)}` : ""}` }];
      }
      const at = r && r.lease && epochOf(r.lease.expires_at);
      const on = r && r.lease && leaseName(r.lease);           // LEASE-BOARD: mps3_01
      return [{ kind: "ok", text: `${name} is yours: lease held${on ? ` on ${on}` : ""}${at ? ` until ${clock(at)}` : ""}` }];
    },
    onDone: () => afterLease(bid),
  };
}

function sendRequest() {
  const f = L.form;
  if (!f) return;
  const bid = f.bid;
  const off = notesOff(bid);
  const message = off ? "" : f.message.trim();
  const spec = requestSpec(bid, message, off ? 0 : f.want);
  // G3: a board that is not open may be requested too (the preview); only a job stops it.
  const open = !!(S.boards[bid] && S.boards[bid].open);
  const job = !open && S.board[bid] && S.board[bid].job;
  const why = open ? gateReason(bid, "lease_req", spec.key, {})
    : job ? `waiting for the ${job.kind === "lease_request" ? "request already out" : "job on this board"}` : "";
  closeForm();
  delete L.resultDismissed[bid];
  if (why) {
    interlock(bid, "lease_req", spec.command, why);
    return;
  }
  runAction(bid, "lease_req", spec);
  // The Request board button is gone now: keyboard focus goes to the bar's first action.
  setTimeout(() => {
    const next = document.querySelector('[data-testid="lease-request"] [data-action="lease_leave"]');
    if (next) next.focus();
  }, 80);
}

function leaveSpec(bid) {
  return {
    key: "lease_leave", label: "Leave queue", busyLabel: "Leaving...", budgetS: 30,
    command: `lease leave ${cliTarget(bid)}`,
    run: async () => {
      L.leaving.add(bid);
      try {
        return (await call("leaseLeave", { bid })).data;
      } catch (e) {
        if (routeMissing(e)) {
          // A daemon from before lease requests: DELETE /lease cancels a queued acquire.
          const r = (await call("leaseRelease", { bid })).data;
          return { left: !!(r && r.cancelled) };
        }
        L.leaving.delete(bid);
        throw e;
      }
    },
    render: (r) => [{ kind: "ok", text: r && r.left
      ? "you left the queue: the request is withdrawn and the holder is no longer asked"
      : "nothing of yours was queued" }],
    onDone: () => afterLease(bid),
  };
}

// Leave the queue (Cancel request): from the request bar, the queue popover or the preview. A
// board that is not open is left without the board's gate (G3: DELETE .../lease/queue).
export function leaveQueue(bid) {
  const spec = leaveSpec(bid);
  const open = !!(S.boards[bid] && S.boards[bid].open);
  const why = open ? gateReason(bid, "lease_leave", spec.key, { whileJob: true }) : "";
  if (why) interlock(bid, "lease_leave", spec.command, why);
  else runAction(bid, "lease_leave", spec);
}

function forceSpec(bid, confirmBoard = "") {
  const name = leaseBoardName(bid);
  const hub = hubOf(bid);
  const holder = (hub && hub.lease && hub.lease.holder) || "the holder";
  const typed = String(confirmBoard || "").trim();
  return {
    key: "lease_force", label: "Force release", busyLabel: "Force releasing...", budgetS: 120,
    command: typed ? `lease force ${cliTarget(bid)} --confirm-board ${/^[A-Za-z0-9_.:@-]+$/.test(typed) ? typed : shellQuote(typed)}` : `lease force ${cliTarget(bid)} --yes`,
    run: (ctx) => runJob("leaseForce", { bid }, typed ? { confirm: true, confirm_board: typed } : { confirm: true },
      (d) => ctx.progress(d.phase || "revoking", d.phase), "lease_force"),
    render: (r) => {
      const at = r && r.lease && epochOf(r.lease.expires_at);
      return [{ kind: "ok", text: `${holder} was force-released; ${name} is yours${at ? ` until ${clock(at)}` : ""}. Their session is told who took it.` }];
    },
    // D3: 422 "time left" carries when force opens.
    renderError: (e) => {
      const d = (e && e.data) || {};
      if (d.time_left_s === undefined || d.time_left_s === null) return [];
      const at = epochOf(d.deadline_at);
      return [{ kind: "hint", text: `force-release opens in ${mmss(Number(d.time_left_s))}${at ? ` (at ${clock(at)})` : ""}, if there is still no answer` }];
    },
    onDone: () => loadHub(bid),
  };
}

// Why Force is not offered now: the daemon's reason, else what the countdown says.
export function forceWhy(bid) {
  const hub = hubOf(bid);
  const req = hub && hub.request;
  if (!req) return "there is no request of yours to force";
  if (req.force_available) return "";
  const role = revokeOff(bid);               // T8: the token's role, whatever the clock says
  if (role) return role;
  // While the holder's 2:00 run, say it live (the daemon's reason is as old as the read).
  const left = secondsTo(req.deadline_at);
  if (!req.answer && left !== null && left > 0) {
    const holder = (hub.lease && !hub.lease.mine && hub.lease.holder) || "the holder";
    return `available in ${mmss(left)}, if ${holder} has not answered by then`;
  }
  if (req.force_reason) return req.force_reason;
  return "the daemon has not said force is available yet";
}

function openConfirm(bid, trigger) {
  const why = forceWhy(bid);
  if (why) {
    interlock(bid, "lease_force", forceSpec(bid).command, `Cannot force: ${why}`);
    return;
  }
  L.confirm = { bid, trigger, typed: "" };
  changed();
}

function closeConfirm() {
  const t = L.confirm && L.confirm.trigger;
  L.confirm = null;
  changed();
  if (t && t.isConnected) setTimeout(() => t.focus(), 0);
}

function confirmForce() {
  const c = L.confirm;
  if (!c) return;
  const bid = c.bid;
  const script = holderMayBeScript(bid);
  if (script && !nameMatches(c.typed, forceName(bid))) return;     // D12: Force stays disabled
  const spec = forceSpec(bid, script ? c.typed : "");
  const why = forceWhy(bid) || gateReason(bid, "lease_force", spec.key, { whileJob: true });
  closeConfirm();
  if (why) {
    interlock(bid, "lease_force", spec.command, `Cannot force: ${why}`);
    return;
  }
  runAction(bid, "lease_force", spec);
}

function RequestBar({ bid }) {
  const hub = hubOf(bid);
  const pr = panelState(bid, "lease_req");
  const pl = panelState(bid, "lease_leave");
  const pf = panelState(bid, "lease_force");
  const req = hub && hub.request;
  const active = requestActive(bid) || !!pf.running;
  const results = [pr, pf, pl].filter((p) => p.lines.length);
  const latest = Math.max(0, ...results.map((p) => p.startedAt || 0));
  if (!active && (!results.length || L.resultDismissed[bid] === latest)) return null;
  const name = leaseBoardName(bid);
  const holder = hub && hub.lease && !hub.lease.mine ? hub.lease.holder : "";
  const dismiss = () => {
    L.resultDismissed[bid] = latest;
    for (const p of results) if (!p.running) p.lines = [];
    changed();
  };
  const resultBlocks = results.map((p) => html`<${ResultBlock} key=${p.panel} lines=${p.lines} panel=${p}
    testid=${`result-${p.panel}`} />`);
  if (!active) {
    // The request is over: held, withdrawn, or refused. Its result stays until closed.
    const mine = leaseWho(bid).state === "here";
    return html`<div class=${`banner lease-bar ${mine ? "ok" : ""}`} role="status" data-testid="lease-request">
      <${Icon} name=${mine ? "lock" : "info"} />
      <div class="grow">
        <strong data-testid="req-title">${mine ? `${name} is yours.` : `Your request for ${name} is over.`}</strong>
        ${resultBlocks}
        ${mine ? html`<div class="lease-actions"><${ReleaseButton} bid=${bid} /></div>` : null}
      </div>
      <button type="button" class="btn ghost sm icon-only" aria-label="Close this result" title="Close"
        data-action="lease_request_dismiss" onClick=${dismiss}><${Icon} name="x" /></button>
    </div>`;
  }
  const answer = req && req.answer;
  const left = req ? secondsTo(req.deadline_at) : null;
  const keepEnd = keepUntil(answer);
  const keepLeft = keepEnd === null ? null : keepEnd - Date.now() / 1000;
  const why = forceWhy(bid);
  const canForce = !!req && !why;
  const forceBusy = !!pf.running;
  let clockText = null;
  if (req && !answer && left !== null) {
    clockText = left > 0
      ? html`<span class="lease-clock" data-testid="req-countdown" data-left=${Math.ceil(left)}><${Icon} name="timer" cls="sm" />${mmss(left)}</span> left for them to answer`
      : html`<span class="lease-clock due" data-testid="req-countdown" data-left="0"><${Icon} name="timer" cls="sm" />0:00</span> no answer in time`;
  }
  return html`<div class=${`banner lease-bar ${canForce ? "due" : "info"}`} role="status" aria-live="polite" data-testid="lease-request">
    <${Icon} name="send" />
    <div class="grow">
      <div class="lease-line">
        <strong data-testid="req-title">${req ? `You asked for ${name}` : pf.running ? `Force releasing ${name}...` : `Asking for ${name}...`}${holder ? ` (held by ${holder})` : ""}</strong>
        ${req && req.position ? html`<span class="lease-fact" data-testid="req-position">position ${req.position} in the queue</span>` : null}
        ${req ? html`<span class="lease-fact" data-testid="req-asked"><${Icon} name="check" cls="sm" />the holder has been asked</span>`
          : html`<span class="lease-fact muted"><${Spinner} /> joining the queue</span>`}
        ${clockText ? html`<span class="lease-fact">${clockText}</span>` : null}
      </div>
      ${L.reasked[bid] && req ? html`<div class="lease-answer" data-testid="req-reasked">
        <${Icon} name="refresh-cw" cls="sm" /><span><strong>${name} passed to ${L.reasked[bid].holder}</strong>, who had not
        been asked: your request went to them, and a new 2:00 runs to ${clock(epochOf(req.deadline_at))}.</span></div>` : null}
      ${answer && answer.answer === "keep" ? html`<div class="lease-answer" data-testid="req-answer">
        <${Icon} name="clock" cls="sm" /><span><strong>${holder || "The holder"} is keeping it for ${answer.minutes} min</strong>${answer.message ? html`: ${quoted(answer.message)}` : null}
        ${keepLeft !== null ? (keepLeft > 0
          ? html`<span class="lease-fact" data-testid="req-keep-left">${minutesLeft(keepLeft)} min left${keepEnd ? ` (until ${clock(keepEnd)})` : ""}</span>`
          : html`<span class="lease-fact" data-testid="req-keep-left">their ${answer.minutes} min ran out</span>`) : null}</span>
      </div>` : null}
      ${req && req.message ? html`<div class="secondary small">Your message: ${quoted(req.message)}</div>` : null}
      <${TappedLine} at=${req && req.tapped_at} testid="req-tapped"
        text=${`someone at ${name} tapped your request on its front panel; the holder is told. Nothing was released.`} />
      <div class="lease-actions">
        <button type="button" class="btn sm" data-action="lease_leave" aria-busy=${pl.running ? "true" : undefined}
          onClick=${() => {
            const spec = leaveSpec(bid);
            const w = gateReason(bid, "lease_leave", spec.key, { whileJob: true });
            if (w) interlock(bid, "lease_leave", spec.command, w); else runAction(bid, "lease_leave", spec);
          }}>${pl.running ? html`<${Spinner} /> Leaving...` : html`<${Icon} name="circle-minus" /> Leave queue`}</button>
        <button type="button" class=${`btn sm ${canForce ? "danger-solid" : "danger"}`} data-action="lease_force_open"
          aria-disabled=${canForce && !forceBusy ? undefined : "true"} aria-busy=${forceBusy ? "true" : undefined}
          aria-haspopup="dialog" title=${why || "Kick the holder off the board now (asks first)"}
          onClick=${(e) => { if (!forceBusy) openConfirm(bid, e.currentTarget); }}>
          ${forceBusy ? html`<${Spinner} /> Force releasing...` : html`<${Icon} name="zap" /> Force release…`}</button>
        ${why ? html`<${Reason} text=${`Force: ${why}`} icon="circle-slash" testid="reason-lease_force" />` : null}
      </div>
      ${resultBlocks}
    </div>
  </div>`;
}

// --- the holder ---------------------------------------------------------------------------------

function answerOf(bid, id) {
  return (L.answers[bid] || {})[id] || null;
}

function saveAnswer(bid, id, a) {
  L.answers[bid] = { ...(L.answers[bid] || {}), [id]: a };
}

function respond(bid, note, answer, minutes, message) {
  const panel = `lease_respond:${note.id}`;
  const target = leaseBoardName(bid);
  const flags = answer === "release" ? "--release" : `--keep ${minutes}`;
  const spec = {
    key: `respond_${answer}`, label: answer === "release" ? "Release now" : `Keep ${minutes} min`,
    busyLabel: "Answering...", budgetS: 30,
    command: `lease respond ${cliTarget(bid)} ${note.id} ${flags}${message ? ` --message ${shellQuote(message)}` : ""}`,
    run: async () => (await call("leaseRespond", { bid }, {
      id: note.id, answer, ...(answer === "keep" ? { minutes } : {}), ...(message ? { message } : {}),
    })).data,
    render: () => [{ kind: "ok", text: answer === "release"
      ? `released: ${note.by} gets ${target} next`
      : `${note.by} is told you keep it for ${minutes} min` }],
    onDone: (ok) => {
      if (ok) saveAnswer(bid, note.id, { answer, minutes: answer === "keep" ? minutes : 0, message,
        at: new Date().toISOString(), by: note.by });
      loadHub(bid);
    },
  };
  const why = gateReason(bid, panel, spec.key, { whileJob: true });
  if (why) {
    interlock(bid, panel, spec.command, why);
    return;
  }
  runAction(bid, panel, spec);
}

// CCR PANEL-1 (lane P3): a tap on the front panel's lease-request banner records tapped_at on
// the open request (GET /lease: incoming[].tapped_at for the holder, request.tapped_at for the
// requester). Decision P2: it notifies the holder; it never releases.
function TappedLine({ at, text, testid }) {
  const when = epochOf(at);
  if (when === null) return null;
  return html`<div class="lease-tapped small" data-testid=${testid} data-at=${at}>
    <${Icon} name="user" cls="sm" /><span><strong>Tapped on the panel</strong> at ${clock(when)}: ${text}</span></div>`;
}

function HolderPrompt({ bid, note }) {
  const [message, setMessage] = useState("");
  const p = panelState(bid, `lease_respond:${note.id}`);
  const name = leaseBoardName(bid);
  const left = secondsTo(note.deadline_at);
  const busy = !!p.running;
  const off = notesOff(bid);
  return html`<div class="banner lease-prompt" role="alert" data-testid="lease-wanted" data-request=${note.id} data-board=${bid}>
    <${Icon} name="triangle-alert" />
    <div class="grow">
      <div class="lease-prompt-title" data-testid="wanted-title"><strong>${note.by}</strong> wants <strong>${name}</strong>${note.message
        ? html`: ${quoted(note.message)}` : off ? "." : html`. <span class="muted">(no message)</span>`}</div>
      <div class="secondary small">
        ${left === null ? "Answer now, or they may force-release it."
          : left > 0 ? html`Answer within <span class="lease-clock" data-testid="wanted-countdown" data-left=${Math.ceil(left)}><${Icon} name="timer" cls="sm" />${mmss(left)}</span>, or they may force-release it: anything you run on the board is interrupted then.`
          : html`<span class="lease-clock due" data-testid="wanted-countdown" data-left="0"><${Icon} name="timer" cls="sm" />0:00</span> Their 2 minutes are up: they may force-release it now.`}
      </div>
      <${TappedLine} at=${note.tapped_at} testid="wanted-tapped"
        text=${`someone at ${name} tapped this request on its front panel. Nothing was released: the answer is still yours.`} />
      <div class="lease-actions">
        ${off ? null : html`<input class="input lease-msg" type="text" maxlength=${NOTE_MAX} placeholder="Message (optional)"
          aria-label=${`Message to ${note.by} (optional)`} data-testid="wanted-message" value=${message}
          onInput=${(e) => setMessage(e.target.value)} disabled=${busy} />`}
        <button type="button" class="btn sm primary" data-action="respond_release" disabled=${busy}
          onClick=${() => respond(bid, note, "release", 0, off ? "" : message.trim())}><${Icon} name="lock-open" /> Release now</button>
        ${off ? null : html`<span class="keep-group" role="group" aria-label="Keep it for">
          <span class="keep-label">Keep for</span>
          ${KEEP_MINUTES.map((m) => html`<button type="button" key=${m} class="btn sm" data-action=${`respond_keep_${m}`}
            disabled=${busy} onClick=${() => respond(bid, note, "keep", m, message.trim())}>${m} min</button>`)}
        </span>`}
        ${busy ? html`<span class="muted small"><${Spinner} /> answering</span>` : null}
      </div>
      ${off ? html`<${Reason} icon="circle-slash" testid="wanted-notes-off"
        text=${`No message and no "keep for N min" here: ${off}. Release now still works; to keep the board, tell ${note.by} another way.`} />` : null}
      ${p.lines.length ? html`<${ResultBlock} lines=${p.lines} panel=${p} testid="result-lease_respond" />` : null}
    </div>
  </div>`;
}

function AnsweredLine({ bid, id, a }) {
  const p = panelState(bid, `lease_respond:${id}`);
  const at = epochOf(a.at);
  const until = keepUntil(a);
  const board = leaseBoardName(bid);
  const close = () => { L.answerDismissed.add(`${bid}|${id}`); changed(); };
  return html`<div class="banner lease-bar ok" role="status" data-testid="lease-answered" data-request=${id}>
    <${Icon} name="circle-check" />
    <div class="grow">
      <strong>${a.answer === "release"
        ? `You released ${board} to ${a.by}${at ? ` at ${clock(at)}` : ""}.`
        : `You answered ${a.by}: keep ${board} for ${a.minutes} min${a.message ? "" : "."}`}</strong>${a.answer === "keep" && a.message
        ? html` ${quoted(a.message)}` : null}
      ${until ? html` <span class="secondary">They may force-release it after ${clock(until)}.</span>` : null}
      ${p.lines.length ? html`<${ResultBlock} lines=${p.lines} panel=${p} testid="result-lease_respond" />` : null}
    </div>
    <button type="button" class="btn ghost sm icon-only" aria-label="Close this answer" title="Close"
      data-action="lease_answer_dismiss" onClick=${close}><${Icon} name="x" /></button>
  </div>`;
}

function HolderPrompts({ bid }) {
  const hub = hubOf(bid);
  const incoming = (hub && hub.incoming) || [];
  const out = [];
  const ids = new Set();
  const shown = new Set();
  for (const note of incoming) {
    ids.add(note.id);
    // D5: the daemon reads our answer from the hub's answer note, whoever gave it (this page,
    // another page, the CLI); this page's own answer covers a daemon without D5.
    const a = note.answer ? { ...note.answer, by: note.by } : answerOf(bid, note.id);
    if (!a) {
      out.push(html`<${HolderPrompt} key=${`w-${bid}-${note.id}`} bid=${bid} note=${note} />`);
    } else if (!L.answerDismissed.has(`${bid}|${note.id}`)) {
      shown.add(note.id);
      out.push(html`<${AnsweredLine} key=${`a-${bid}-${note.id}`} bid=${bid} id=${note.id} a=${a} />`);
    }
  }
  // A request answered here that has gone since (a release: the requester has the board)
  // stays in view until closed, for an hour at most.
  for (const [id, a] of Object.entries(L.answers[bid] || {})) {
    if (shown.has(id) || ids.has(id) || L.answerDismissed.has(`${bid}|${id}`)) continue;
    if (Date.now() / 1000 - (epochOf(a.at) || 0) > 3600) continue;
    out.push(html`<${AnsweredLine} key=${`a-${bid}-${id}`} bid=${bid} id=${id} a=${a} />`);
  }
  return out;
}

// --- the victim ------------------------------------------------------------------------------------

function takenText(bid, t) {
  const at = epochOf(t.at);
  return `${leaseBoardName(bid)} was force-released by ${t.by || "another hub user"}${at ? ` at ${clock(at)}` : ""}${t.reason ? `: ${t.reason}` : ""}`;
}

function takenKey(t) { return String((t && (t.at || t.reason)) || ""); }

function noteTaken(bid, t) {
  if (!t || L.dismissedTaken[bid] === takenKey(t)) return;
  const k = `${bid}|${takenKey(t)}`;
  if (L.takenLogged.has(k)) return;
  L.takenLogged.add(k);
  log("error", "lease", takenText(bid, t), bid);
}

// D11: DELETE .../lease/taken makes the daemon forget it (GET /lease then says taken: null).
// The page also remembers the dismissal, so the banner goes at once, and stays gone over a
// daemon without the route.
async function dismissTaken(bid, t) {
  L.dismissedTaken[bid] = takenKey(t);
  remember(localStore(), DISMISSED_KEY, L.dismissedTaken);
  changed();
  const r = await timed("lease taken dismiss", () => call("leaseTakenDismiss", { bid }));
  if (r.error && routeMissing(r.error)) {
    log("info", "lease", `${r.line}: this daemon keeps no dismissal; this browser remembers it`, bid);
  } else if (r.error) {
    log("warning", "lease", `${r.line}  ${r.error.errName}: ${r.error.message}`, bid);
  } else {
    log("info", "lease", `${r.line}  the force-release notice for ${leaseBoardName(bid)} is dismissed`, bid);
    loadHub(bid);
  }
}

function TakenBanner({ bid }) {
  const hub = hubOf(bid);
  const t = hub && hub.taken;
  if (!t || L.dismissedTaken[bid] === takenKey(t)) return null;
  return html`<div class="banner err" role="alert" data-testid="lease-taken" data-board=${bid}>
    <${Icon} name="octagon-x" />
    <div class="grow"><strong>${takenText(bid, t)}</strong>
      <div class="secondary small mt-8">The lease is no longer yours: do not drive the board until you have it again.</div></div>
    <button type="button" class="btn sm" data-action="lease_taken_dismiss" onClick=${() => dismissTaken(bid, t)}>Dismiss</button>
  </div>`;
}

// --- the modals ---------------------------------------------------------------------------------------

// Escape closes; Tab stays inside the dialog; the [data-autofocus] element takes focus on
// open (the autofocus attribute works once per document, not for a dialog added later).
// Layout effects: both are in place as soon as the dialog is in the DOM, so a key pressed
// right after the click that opened it is never lost.
function useDialogKeys(ref, onEscape) {
  useLayoutEffect(() => {
    const first = ref.current && ref.current.querySelector("[data-autofocus]");
    if (first) first.focus();
  }, []);
  useLayoutEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); onEscape(); return; }
      if (e.key !== "Tab" || !ref.current) return;
      const f = [...ref.current.querySelectorAll("button, input, textarea, [tabindex]:not([tabindex='-1'])")]
        .filter((el) => !el.disabled);
      if (!f.length) return;
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

// Request <board> (round 3, M3): who holds it and for how long, where you would join the queue
// (after the people waiting; automation waits behind you), a message, and how long you want it
// (want_s, G11). Over a hub connection without notes (REST) neither reaches the holder: said.
function RequestForm() {
  const ref = useRef(null);
  useDialogKeys(ref, closeForm);
  const f = L.form;
  const bid = f.bid;
  const who = leaseView(bid);
  const holder = who.state === "other" || who.state === "elsewhere" ? who.holder : "";
  const left = leaseLeft(who.lease);
  const name = leaseBoardName(bid);
  const off = notesOff(bid);
  const q = queueOf(bid);
  const ahead = q ? q.people.filter((x) => !x.you) : [];
  const bots = q ? q.bots.length : 0;
  const pos = ahead.length + 1;
  const short = (w) => String(w || "").split("@")[0];
  const aheadText = ahead.map((x) => `${short(x.who)}${x.want ? ` (${x.want})` : ""}`).join(", ");
  return html`<div class="modal-back" onClick=${(e) => { if (e.target === e.currentTarget) closeForm(); }}>
    <form class="modal mid" role="dialog" aria-modal="true" aria-labelledby="lease-form-title" ref=${ref}
      data-testid="lease-request-form" data-board=${bid} onSubmit=${(e) => { e.preventDefault(); sendRequest(); }}>
      <div class="modal-head"><${Icon} name="send" /><h2 class="card-title" id="lease-form-title">Request ${name}</h2>
        <span class="grow"></span>
        <button type="button" class="btn ghost sm icon-only" aria-label="Close" tabindex="-1" onClick=${closeForm}><${Icon} name="x" /></button></div>
      <div class="modal-pad">
        <p data-testid="request-what">${holder ? html`<strong>${holder}</strong> holds it${left !== null ? ` (${leftText(left)} left)` : ""}.` : "Someone else holds it."}
          ${off ? " The hub queues you" : html` Your request reaches ${short(holder) || "the holder"}'s Harness Manager and the board's front panel; the hub queues you`}
          ${q ? html` as <b data-testid="request-position">#${pos}</b>, after ${aheadText || "nobody"}.` : "."}
          ${bots ? ` ${bots === 1 ? "One automation run waits" : `${bots} automation runs wait`} behind you: the hub serves people first.` : ""}</p>
        <p class="secondary small">With no answer in 2 minutes you may force-release it. Requesting does not open or lock the board.</p>
        ${q && (q.people.length || q.bots.length) ? html`<div class="lq-inmodal"><div class="field-label">In the queue now</div>
          <${LeaseQueueList} q=${q} compact=${true} testid="request-queue" /></div>` : null}
        ${off ? html`<${Reason} icon="circle-slash" testid="request-notes-off"
          text=${`No message: ${off}. ${holder || "The holder"} sees that you are waiting, not why or for how long.`} />`
        : html`<label class="field-label" for="lease-message">Message (optional)</label>
          <textarea id="lease-message" class="input lease-textarea" rows="3" maxlength=${NOTE_MAX}
            placeholder="Why you need it" value=${f.message} data-autofocus
            onInput=${(e) => { f.message = e.target.value; }}></textarea>
          <div class="field" data-testid="request-want"><label>How long</label>
            <${Seg} label="How long you want the board" value=${f.want}
              options=${WANT_CHOICES.map((c) => ({ ...c, title: `You want it for ${c.label}: ${short(holder) || "the holder"} sees it` }))}
              onChange=${(v) => { f.want = v; changed(); }} /></div>`}
      </div>
      <div class="modal-foot">
        <button type="button" class="btn" data-action="lease_request_cancel" onClick=${closeForm}>Cancel</button>
        <button type="submit" class="btn primary" data-action="lease_request" data-autofocus=${off ? true : undefined}><${Icon} name="send" /> Send request</button>
      </div>
    </form>
  </div>`;
}

function ForceConfirm() {
  const ref = useRef(null);
  useDialogKeys(ref, closeConfirm);
  const bid = L.confirm.bid;
  const hub = hubOf(bid);
  const holder = (hub && hub.lease && hub.lease.holder) || "the holder";
  const name = leaseBoardName(bid);
  const req = hub && hub.request;
  const why = forceWhy(bid);
  const made = req && epochOf(req.created_at);
  // D4: the revoke acts on the physical board; say so when it is not the target's name.
  const target = hub && hub.lease && hub.lease.target;
  const board = hub && hub.board && hub.board !== target ? hub.board : "";
  // D12: nobody can say a Harness Manager session holds it: the board's name, typed.
  const script = holderMayBeScript(bid);
  const typeName = forceName(bid);
  const matched = !script || nameMatches(L.confirm.typed, typeName);
  const disarmed = !!why || !matched;
  return html`<div class="modal-back" onClick=${(e) => { if (e.target === e.currentTarget) closeConfirm(); }}>
    <div class="modal small danger" role="alertdialog" aria-modal="true" aria-labelledby="force-title"
      aria-describedby="force-what" ref=${ref} data-testid="force-confirm">
      <div class="modal-head"><${Icon} name="zap" /><h2 class="card-title" id="force-title">Force release ${name}?</h2></div>
      <div class="modal-pad">
        <p id="force-what" class="force-what" data-testid="force-what">This kicks <strong>${holder}</strong> off <strong>${name}</strong> now;
          anything they are running is interrupted.</p>
        ${board ? html`<p class="secondary small" data-testid="force-board">It revokes board <code>${board}</code>${target
          ? html` (the hub target <code>${target}</code> is part of it)` : null}.</p>` : null}
        <p class="secondary small">The hub records who did it and why${made ? ` (no answer to your request made at ${clock(made)})` : ""},
          and ${holder}'s session is told who took the board.</p>
        ${script ? html`<div data-testid="force-script">
          <${Reason} level="warn" icon="triangle-alert" testid="force-script-why"
            text=${`No Harness Manager session is known to hold ${typeName}; it may be a script (a soak or runner). Type ${typeName} to force-release.`} />
          ${hub.lease.holder_kind_reason ? html`<p class="secondary small" data-testid="force-script-reason">${hub.lease.holder_kind_reason}.</p>` : null}
          <label class="field-label" for="force-board-name">Board name</label>
          <input id="force-board-name" class="input mono force-name" type="text" autocomplete="off" spellcheck="false"
            data-testid="force-board-name" value=${L.confirm.typed} data-autofocus
            onInput=${(e) => { L.confirm.typed = e.target.value; changed(); }}
            onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); if (matched) confirmForce(); } }} />
        </div>` : null}
        ${why ? html`<${Reason} level="err" text=${`Not available now: ${why}`} testid="force-confirm-why" />` : null}
      </div>
      <div class="modal-foot">
        <button type="button" class="btn" data-action="force_cancel" onClick=${closeConfirm} data-autofocus=${script ? undefined : true}>Cancel</button>
        <button type="button" class="btn danger-solid" data-action="force_confirm"
          aria-disabled=${disarmed ? "true" : undefined}
          title=${!why && !matched ? `Type ${typeName} first` : undefined}
          onClick=${confirmForce}><${Icon} name="zap" /> Force</button>
      </div>
    </div>
  </div>`;
}

// --- LEASE-UI: whose lease it is, Release, and closing a board you hold ------------------------------
//
// "Yours" is `here` (THIS Harness Manager holds the lease token), never `mine`: every lab
// session shares one fpgahub principal, so a lease another session or a soak took under it is
// "held by david@mapstone-dev (another session)" (leaseWho, week.js).

// "Held by alice@lab-pc-07"; the same principal elsewhere says so.
export function heldText(who) {
  return who.state === "elsewhere" ? `Held by ${who.holder} (another session)` : `Held by ${who.holder}`;
}

// The rail's badge: an icon and words (never colour alone). Only what the page already read
// (GET /lease for an open board behind a hub): a board with no hub, or not read, has none.
export function LeaseBadge({ bid, prefix = "rail" }) {
  const who = leaseWho(bid);
  if (who.state === "none" || who.state === "unread") return null;
  let chip;
  if (who.state === "here") {
    const left = leaseLeft(who.lease);
    chip = html`<${Chip} level="ok" icon="user" cls="lease-badge" testid=${`${prefix}-lease-badge`}
      title=${`Your hub lease: ${leaseWhere(who)}, held by this Harness Manager${left !== null ? `, ${durationText(left)} left` : ""}${who.stale ? `; ${staleNote(who.stale)}` : ""}`}>Yours<//>`;
  } else if (who.state === "free") {
    chip = html`<${Chip} icon="lock-open" cls="lease-badge" testid=${`${prefix}-lease-badge`}
      title=${`Free: nobody holds ${leaseWhere(who)}`}>Free<//>`;
  } else if (who.state === "unknown") {
    chip = html`<${Chip} level="unk" icon="circle-help" cls="lease-badge" testid=${`${prefix}-lease-badge`}
      title=${`The lease could not be read from ${who.host || "the hub"}: ${who.error}`}>Lease unknown<//>`;
  } else {
    const text = heldText(who);
    chip = html`<${Chip} level="held" icon="lock" cls="lease-badge" testid=${`${prefix}-lease-badge`}
      title=${`${text}: ${leaseWhere(who)}${who.state === "elsewhere"
        ? ". Your hub name, but not this Harness Manager: another session, or a script you run" : ""}`}>
      <span class="lease-badge-text">${text}</span><//>`;
  }
  const queue = who.queued && who.state !== "here"
    ? html`<${Chip} level="accent" icon=${who.requested ? "send" : "clock"} cls="lease-badge" testid=${`${prefix}-lease-queued`}
        title=${who.requested ? "You asked for it: your request is in the queue" : "You are queued for it"}>
        ${who.requested ? "Requested" : "Queued"}${who.position ? ` · #${who.position}` : ""}<//>` : null;
  return html`<div class="board-lease" data-testid=${`${prefix}-lease-row`} data-lease=${who.state}>${chip}${queue}</div>`;
}

// Release: prominent wherever a lease you hold is shown; it always asks first.
export function ReleaseButton({ bid, compact = true }) {
  const p = panelState(bid, "lease");
  const running = p.running === "lease_release";
  return html`<button type="button" class=${`btn release ${compact ? "sm" : ""}`} data-action="lease_release_open"
    aria-haspopup="dialog" aria-busy=${running ? "true" : undefined}
    title="Give the hub lease back so others can take the board (asks first)"
    onClick=${(e) => { if (!running) openReleaseConfirm(bid, e.currentTarget); }}>
    ${running ? html`<${Spinner} /> Releasing...` : html`<${Icon} name="lock-open" /> Release lease`}</button>`;
}

export function openReleaseConfirm(bid, trigger = null) {
  L.release = { bid, trigger: trigger || document.activeElement };
  changed();
}

function closeRelease() {
  const t = L.release && L.release.trigger;
  L.release = null;
  changed();
  if (t && t.isConnected) setTimeout(() => t.focus(), 0);
}

// Why Release cannot run now ("" when it can): a job on the board (releasing mid-deploy hands
// a half-programmed board to the next person), or a lease action already running.
function releaseWhy(bid) {
  return gateReason(bid, "lease", "lease_release", {});
}

function confirmRelease() {
  const r = L.release;
  if (!r || releaseWhy(r.bid)) return;          // the dialog says why; nothing runs
  closeRelease();
  runAction(r.bid, "lease", leaseSpecs(r.bid).release);
}

// Who gets the board next when this lease goes: the first one queued who is not us.
function nextHolder(who) {
  const q = ((who.hub && who.hub.queue) || []).find((e) => e && !e.mine && e.holder);
  return q ? q.holder : "";
}

function ReleaseConfirm() {
  const ref = useRef(null);
  useDialogKeys(ref, closeRelease);
  const bid = L.release.bid;
  const who = leaseWho(bid);
  const target = leaseName(who) || leaseBoardName(bid);        // LEASE-BOARD: mps3_01
  const note = leaseTargetNote(who);
  const next = nextHolder(who);
  const why = releaseWhy(bid);
  return html`<div class="modal-back" onClick=${(e) => { if (e.target === e.currentTarget) closeRelease(); }}>
    <div class="modal small" role="alertdialog" aria-modal="true" aria-labelledby="release-title"
      aria-describedby="release-what" ref=${ref} data-testid="release-confirm" data-board=${bid}>
      <div class="modal-head"><${Icon} name="lock-open" /><h2 class="card-title" id="release-title"
        data-testid="release-title">Release ${target}?</h2></div>
      <div class="modal-pad">
        <p id="release-what" data-testid="release-what">Others can take it${next
          ? html`: <strong>${next}</strong> is next in the queue and gets it` : null}; background checks pause.</p>
        ${note ? html`<p class="secondary small" data-testid="release-target">${"The hub leases it as target "}<code>${note}</code>.</p>` : null}
        <p class="secondary small">${leaseBoardName(bid)} stays open here. Once someone else holds the lease,
          this page reads the board only when you click, and nothing you run should drive it until the lease
          is yours again (Acquire lease, or Request board).</p>
        ${why ? html`<${Reason} level="warn" icon="circle-slash" testid="release-why" text=${`Not now: ${why}.`} />` : null}
      </div>
      <div class="modal-foot">
        <button type="button" class="btn" data-action="release_cancel" onClick=${closeRelease} data-autofocus>Cancel</button>
        <button type="button" class="btn primary" data-action="release_confirm" onClick=${confirmRelease}
          aria-disabled=${why ? "true" : undefined} title=${why || undefined}>
          <${Icon} name="lock-open" /> Release</button>
      </div>
    </div>
  </div>`;
}

// Close board (round 3, M1): always asks what should happen to the board. A board whose lease
// THIS Harness Manager holds offers "Restore baseline, release and close" (the default when a
// design other than the baseline is loaded: the next person finds a clean board), "Release and
// close", or "Close and keep the lease"; a board with no hub, Close or "Restore baseline and
// close"; anyone else's hub board, Close (the lease stays where it is). Restoring is Restore
// baseline (POST .../restore, the lease holder's, R3), then DELETE /boards/{bid}?release=.
// `doClose({release})` is the header's close; it resolves to the timed() result.
export function requestClose(bid, trigger, doClose) {
  L.closing = { bid, trigger: trigger || document.activeElement, doClose, busy: "", error: null, choice: "" };
  changed();
  return null;
}

function endClosing() {
  const t = L.closing && L.closing.trigger;
  L.closing = null;
  changed();
  if (t && t.isConnected) setTimeout(() => t.focus(), 0);
}

// The baseline (rm_id 0: the greybox on the MPS3) and whether it is what the board runs.
function isZeroId(v) { return /^(0x)?0+$/i.test(String(v || "").trim()); }

function baselineName(b) {
  const o = b && b.overlays;
  const ref = o && [...(o.loadable || []), ...(o.all || [])].find((x) => isZeroId(x.rm_id));
  return (ref && ref.name) || "the baseline";
}

export function closeOptions(bid) {
  const b = S.board[bid] || {};
  const ident = (b.info && b.info.identity) || null;
  const who = leaseWho(bid);
  const name = leaseBoardName(bid);
  const base = baselineName(b);
  const design = ident ? ident.rm_name || ident.rm_id || "the design" : "";
  const loaded = ident ? !isZeroId(ident.rm_id) && ident.rm_name !== base : null;   // null: not read
  const job = b.job || null;
  const restoreWhy = job ? `waiting for the ${job.kind === "deploy" ? "deploy" : "job"} on this board`
    : gateReason(bid, "close", "restore", { capability: "deploy_partial", holder: "Restore baseline" });
  const at = epochOf(who.lease && who.lease.expires_at);
  const opts = [];
  if (who.state === "here") {
    const next = nextHolder(who);
    opts.push({ k: "restore", t: "Restore baseline, release and close", dis: loaded === false ? `${base} is loaded already` : restoreWhy,
      said: loaded === false, s: loaded === false ? `${base} is loaded already.`
        : `Loads ${base}${loaded === null ? " (the design loaded now was not read)" : ` in place of ${design}`}, gives the lease back to the hub, closes ${name}. The next person finds a clean board.` });
    const on = leaseName(who) || name;
    const note = leaseTargetNote(who);
    opts.push({ k: "release", t: "Release and close", testid: "close-release-what",
      s: `${loaded ? `Leaves ${design} loaded. ` : ""}The lease on ${on}${note ? ` (target ${note})` : ""} goes back to the hub now${next ? `: ${next} is next and gets it` : ""}.` });
    opts.push({ k: "keep", t: "Close and keep the lease", testid: "close-keep-what",
      s: `It stays yours${at ? ` until ${clock(at)}` : ""}, but nothing renews it while the board is closed. Open the board again to keep renewing it.` });
  } else if (who.state === "none") {
    opts.push({ k: "close", t: "Close", s: `${loaded ? `Leaves ${design} loaded. ` : ""}This board has no hub lease.` });
    if (loaded !== false) {
      opts.push({ k: "restoreclose", t: "Restore baseline and close", dis: restoreWhy,
        s: `Loads ${base}${loaded ? ` in place of ${design}` : ""}, then closes.` });
    }
  } else {
    const req = who.requested || requestActive(bid);
    opts.push({ k: "close", t: "Close the board",
      s: who.state === "other" ? `The lease stays with ${who.holder}.${req ? " Your request stays in the queue." : ""}`
        : who.state === "elsewhere" ? `${who.holder} keeps the lease in its other session.${req ? " Your request stays in the queue." : ""}`
        : who.state === "free" ? "Nobody holds the lease." : "The hub lease is left as it is." });
    // R3: restoring drives the board, so it is the lease holder's: offered, and refused with why
    if (loaded !== false) {
      opts.push({ k: "restoreclose", t: "Restore baseline and close", dis: restoreWhy,
        s: `Loads ${base}${loaded ? ` in place of ${design}` : ""}, then closes.` });
    }
  }
  const def = who.state === "here" ? (loaded && !opts[0].dis ? "restore" : "release") : "close";
  return { opts, def, name, loaded, design, base };
}

async function closeWith(choice) {
  const c = L.closing;
  if (!c || c.busy) return;
  const bid = c.bid;
  const restoring = choice === "restore" || choice === "restoreclose";
  c.busy = choice;
  c.error = null;
  changed();
  if (restoring) {
    const spec = {
      key: "restore", label: "Restore baseline", busyLabel: "Restoring...", budgetS: 180, command: "restore",
      run: (ctx) => runJob("restore", { bid }, undefined, (d) => ctx.progress(d.phase || "restoring", d.phase), "restore"),
      render: () => [{ kind: "ok", text: "the baseline is loaded" }],
    };
    const why = gateReason(bid, "close", spec.key, { capability: "deploy_partial", holder: "Restore baseline" });
    if (why) {
      interlock(bid, "close", spec.command, why);
      c.busy = "";
      c.error = { line: "$ restore  (refused, not run)", error: { errName: "REFUSED", message: `${why}. Nothing was run` } };
      changed();
      return;
    }
    const r = await runAction(bid, "close", spec);
    if (L.closing !== c) return;
    if (!r || !r.ok) {
      const e = r && r.value;
      c.busy = "";
      c.error = { line: "$ restore", error: { errName: (e && e.errName) || "FAILED", message: (e && e.message) || "the restore failed" } };
      changed();
      return;
    }
  }
  const before = S.board[bid] && S.board[bid].info && S.board[bid].info.identity;
  const r = await c.doClose({ release: choice === "restore" || choice === "release" });
  if (L.closing !== c) return;
  if (!(r && r.error) && before) {
    // what the preview shows as loaded: this page's last read, the baseline when it restored
    const base = baselineName(S.board[bid] || {});
    LAST_SEEN[bid] = { at: Date.now() / 1000, identity: restoring
      ? { ...before, rm_id: "0x00000000", rm_name: base === "the baseline" ? before.rm_name : base } : { ...before } };
  }
  if (r && r.error) {
    c.busy = "";
    c.error = r;              // the board stays open: say why, here, and let them choose again
    changed();
    return;
  }
  L.closing = null;           // the board closed: its header (and the trigger) are gone
  changed();
}

const CLOSE_GROUP = "close-choice";     // the radios' group (a variable: t14's icon scan reads name="…")

function CloseConfirm() {
  const ref = useRef(null);
  const c = L.closing;
  useDialogKeys(ref, () => { if (!L.closing || !L.closing.busy) endClosing(); });
  const bid = c.bid;
  const b = S.board[bid] || {};
  const { opts, def, name, loaded, design } = closeOptions(bid);
  const pick = opts.find((o) => o.k === c.choice && !o.dis) ? c.choice : def;
  const chosen = opts.find((o) => o.k === pick) || opts[0];
  const busy = c.busy;
  const err = c.error && c.error.error;
  const bar = busy && busy.startsWith("restore") ? deployBar(b.deploy) : null;
  return html`<div class="modal-back" onClick=${(e) => { if (e.target === e.currentTarget && !busy) endClosing(); }}>
    <div class="modal mid" role="alertdialog" aria-modal="true" aria-labelledby="close-lease-title"
      aria-describedby="close-lease-what" ref=${ref} data-testid="close-confirm" data-board=${bid} data-default=${def}>
      <div class="modal-head"><${Icon} name="panel-left" /><h2 class="card-title" id="close-lease-title"
        data-testid="close-title">Close ${name}</h2></div>
      <div class="modal-pad">
        ${busy && busy.startsWith("restore") ? html`<div class="progress-box" data-testid="close-progress">
            ${bar ? html`<${MiniBar} bar=${bar} />` : null}
            <div class="meter-line"><span>Restoring the baseline on ${name}${busy === "restore" ? ", then releasing the lease" : ""}</span>
              <span class="num">${bar ? bar.line : html`<${Spinner} /> starting`}</span></div></div>`
        : html`<p class="secondary" id="close-lease-what">What should happen to ${name}${loaded ? ` (${design} loaded)` : ""}?</p>
          <div class="choices" role="radiogroup" aria-label="What happens to the board">
          ${opts.map((o) => html`<label key=${o.k} class=${`choice ${pick === o.k ? "on" : ""} ${o.dis ? "dis" : ""}`}
              data-choice=${o.k} data-testid=${`close-choice-${o.k}`} title=${o.dis || undefined}>
            <input type="radio" name=${CLOSE_GROUP} value=${o.k} checked=${pick === o.k} disabled=${!!o.dis || !!busy}
              onChange=${() => { c.choice = o.k; changed(); }} />
            <span><b>${o.t}</b>${o.k === def ? html`<span class="def">default</span>` : null}
              <span class="sub" data-testid=${o.testid || undefined}>${o.s}${o.dis && !o.said ? ` Not now: ${o.dis}.` : ""}</span></span></label>`)}
          </div>`}
        ${err ? html`<${Reason} level="err" testid="close-error"
          text=${`${c.error.line}: ${err.errName}: ${err.message}. The board is still open.`} />` : null}
      </div>
      <div class="modal-foot">
        <button type="button" class="btn ghost" data-action="close_cancel" disabled=${!!busy}
          onClick=${endClosing} data-autofocus>Cancel</button>
        <button type="button" class="btn primary" data-action="close_confirm" data-picked=${pick} disabled=${!!busy}
          aria-busy=${busy ? "true" : undefined} onClick=${() => closeWith(pick)}>
          ${busy ? html`<${Spinner} /> ${busy.startsWith("restore") ? "Restoring..." : busy === "release" ? "Releasing..." : "Closing..."}`
            : chosen.t}</button>
      </div>
    </div>
  </div>`;
}

// --- what the workspace renders ------------------------------------------------------------------------

function openBoards() {
  return S.order.filter((bid) => S.boards[bid] && S.boards[bid].open && hubOf(bid));
}

// Above the header: every open board's victim banner and holder prompts (they matter from
// any board and any section), then the selected board's own request.
export function LeaseBanners({ bid }) {
  const boards = openBoards();
  const others = boards.filter((id) => id !== bid);
  const order = bid && hubOf(bid) ? [bid, ...others] : others;
  return html`
    ${order.map((id) => html`<${TakenBanner} key=${`t-${id}`} bid=${id} />`)}
    ${order.map((id) => html`<${HolderPrompts} key=${`h-${id}`} bid=${id} />`)}
    ${bid && hubOf(bid) ? html`<${RequestBar} bid=${bid} />` : null}
    ${L.form ? html`<${RequestForm} key=${`f-${L.form.bid}`} />` : null}
    ${L.confirm ? html`<${ForceConfirm} key=${`c-${L.confirm.bid}`} />` : null}
    ${L.release ? html`<${ReleaseConfirm} key=${`r-${L.release.bid}`} />` : null}
    ${L.closing ? html`<${CloseConfirm} key=${`x-${L.closing.bid}`} />` : null}`;
}

// --- events, reads and the countdown ticker --------------------------------------------------------

onBoardEvent((ev) => {
  const bid = ev.board_id;
  if (!bid || !ev.topic.startsWith("lease.")) return;
  const d = ev.data || {};
  const w = week(bid);
  if (ev.topic === "lease.wanted" && w.hub && d.id) {
    // Show the prompt now; the read that follows fills in created_at.
    const have = (w.hub.incoming || []).some((n) => n.id === d.id);
    if (!have) w.hub = { ...w.hub, incoming: [...(w.hub.incoming || []), { ...d }] };
  }
  if (ev.topic === "lease.left") w.leaseQueued = false;
  if (ev.topic === "lease.tapped" && w.hub && d.id) {
    // P3: {id, by, at}: someone tapped the panel's request banner; the read that follows agrees.
    const tap = (n) => (n && n.id === d.id ? { ...n, tapped_at: d.at } : n);
    w.hub = { ...w.hub, incoming: (w.hub.incoming || []).map(tap), request: tap(w.hub.request) };
  }
  if (ev.topic === "lease.taken") {
    if (w.hub) w.hub = { ...w.hub, taken: { ...d } };
    noteTaken(bid, d);
  }
  if (["lease.wanted", "lease.answered", "lease.force_available", "lease.taken", "lease.left",
    "lease.tapped"].includes(ev.topic)) {
    scheduleHub(bid, 50);
  }
});

onHubLoaded((bid, hub) => {
  if (hub && hub.taken) noteTaken(bid, hub.taken);
  // UI v2: the header's "N waiting" counts automation too; week.js keeps only the people
  // (CCR SHELL2-2 keeps background_queue there): read the whole queue once per open board.
  if (hub && !Array.isArray(hub.background_queue) && !(FULL[bid] && (FULL[bid].data || FULL[bid].loading))) readLease(bid);
  // D9: the same request with a later deadline means the board passed to someone who had
  // not been asked; the daemon re-sent the request to them and the 2:00 start again.
  const req = hub && hub.request;
  const was = L.seenReq[bid];
  if (!req) {
    delete L.seenReq[bid];
    delete L.reasked[bid];
    return;
  }
  const later = was && was.id === req.id && (epochOf(req.deadline_at) || 0) > (epochOf(was.deadline_at) || 0) + 1;
  if (later) {
    const holder = (hub.lease && !hub.lease.mine && hub.lease.holder) || "the new holder";
    L.reasked[bid] = { holder, deadline_at: req.deadline_at };
    log("warning", "lease", `${leaseBoardName(bid)} passed to ${holder}, who had not been asked: your request went to them; force waits for the new deadline, ${clock(epochOf(req.deadline_at))}`, bid);
  }
  L.seenReq[bid] = { id: req.id, deadline_at: req.deadline_at };
});

// Once a second: re-render the countdowns; read the lease again the moment one reaches
// zero (and 2 s later: the daemon caches GET /lease), and every 10 s while a request is
// out or in.
let ticks = 0;
setInterval(() => {
  ticks += 1;
  let any = false;
  for (const bid of openBoards()) {
    const hub = hubOf(bid);
    const req = hub.request;
    const live = !!(req || (hub.incoming && hub.incoming.length));
    if (!live) continue;
    any = true;
    if (req && !req.force_available) {
      const left = secondsTo(req.deadline_at);
      const keepEnd = keepUntil(req.answer);
      const due = (left !== null && left <= 0 && !req.answer) || (keepEnd !== null && keepEnd <= Date.now() / 1000);
      const key = `${bid}|${req.id}|${req.deadline_at}|${req.answer ? `keep ${req.answer.at}` : "wait"}`;
      if (due && !L.zeroSeen.has(key)) {
        L.zeroSeen.add(key);
        loadHub(bid);
        setTimeout(() => loadHub(bid), 2000);
      }
    }
    if (ticks % 10 === 0 && document.visibilityState === "visible") loadHub(bid);
  }
  if (any) changed();
}, 1000);

// For tests and the devtools console.
window.__harness_managerLease = () => JSON.parse(JSON.stringify({
  form: L.form && { bid: L.form.bid }, confirm: L.confirm && { bid: L.confirm.bid },
  release: L.release && { bid: L.release.bid }, closing: L.closing && { bid: L.closing.bid },
  answers: L.answers, dismissedTaken: L.dismissedTaken,
}));
