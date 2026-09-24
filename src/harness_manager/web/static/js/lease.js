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
// Every countdown runs to a time the daemon read from the hub's note (deadline_at, an
// answer's at + minutes), never to a timer this page started. docs/LEASE_REQUESTS.md D1-D8
// amend the frozen API: a keep answer is a phase of the request job (D1), leaving ends it
// with {left: true} (D7), GET /lease names the physical board (D4) and each incoming
// request's answer (D5). Over fpgahub's REST API (T8, docs/HUB_MODE.md) there are no
// messages and no Keep (notes_supported false), and a non-admin token cannot force.

import { gateReason, interlock, panelState, runAction, runJob } from "./actions.js";
import { call, routeMissing } from "./api.js";
import { boardName, clock, hostOf } from "./format.js";
import { html, useLayoutEffect, useRef, useState } from "./lib.js";
import { changed, log, onBoardEvent, S, timed } from "./store.js";
import { epochOf, loadHub, onHubLoaded, scheduleHub, week } from "./week.js";
import { Icon, Reason, ResultBlock, Spinner } from "./ui.js";

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
  return hub && hub.notesOk === false ? (hub.notesReason || "this hub connection carries no request messages") : "";
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
};

// --- the requester -------------------------------------------------------------------------------

export function requestActive(bid) {
  const hub = hubOf(bid);
  const b = S.board[bid];
  return !!((hub && hub.request) || (b && b.job && b.job.kind === "lease_request")
    || panelState(bid, "lease_req").running);
}

export function openRequestForm(bid, trigger = null) {
  L.form = { bid, message: "", trigger: trigger || document.activeElement };
  changed();
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

function requestSpec(bid, message) {
  const name = leaseBoardName(bid);
  return {
    key: "lease_request", label: "Send request", busyLabel: "Waiting for the board...",
    budgetS: 7200,           // a queue may wait for hours: past this the panel only notes it
    command: `lease request ${cliTarget(bid)}${message ? ` --message ${shellQuote(message)}` : ""}`,
    run: async (ctx) => {
      const onProgress = (d) => ctx.progress(
        (PHASE_TEXT[d.phase] || (() => d.phase || "waiting"))(d), d.phase);
      try {
        return await runJob("leaseRequest", { bid }, message ? { message } : {}, onProgress,
          "lease_request");
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
      return [{ kind: "ok", text: `${name} is yours: lease held${r && r.lease && r.lease.target ? ` on ${r.lease.target}` : ""}${at ? ` until ${clock(at)}` : ""}` }];
    },
    onDone: () => loadHub(bid),
  };
}

function sendRequest() {
  const f = L.form;
  if (!f) return;
  const bid = f.bid;
  const message = notesOff(bid) ? "" : f.message.trim();
  const spec = requestSpec(bid, message);
  const why = gateReason(bid, "lease_req", spec.key, {});
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
    onDone: () => loadHub(bid),
  };
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
    const mine = hub && hub.lease && hub.lease.mine;
    return html`<div class=${`banner lease-bar ${mine ? "ok" : ""}`} role="status" data-testid="lease-request">
      <${Icon} name=${mine ? "lock" : "info"} />
      <div class="grow">
        <strong data-testid="req-title">${mine ? `${name} is yours.` : `Your request for ${name} is over.`}</strong>
        ${resultBlocks}
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

function RequestForm() {
  const ref = useRef(null);
  useDialogKeys(ref, closeForm);
  const f = L.form;
  const hub = hubOf(f.bid);
  const holder = hub && hub.lease && !hub.lease.mine ? hub.lease.holder : "";
  const name = leaseBoardName(f.bid);
  const off = notesOff(f.bid);
  return html`<div class="modal-back" onClick=${(e) => { if (e.target === e.currentTarget) closeForm(); }}>
    <form class="modal small" role="dialog" aria-modal="true" aria-labelledby="lease-form-title" ref=${ref}
      data-testid="lease-request-form" onSubmit=${(e) => { e.preventDefault(); sendRequest(); }}>
      <div class="modal-head"><${Icon} name="send" /><h2 class="card-title" id="lease-form-title">Request ${name}</h2></div>
      <div class="modal-pad">
        <p class="secondary">${holder ? html`<strong>${holder}</strong> holds it.` : "Someone else holds it."} You join the
          queue and they are asked to give it up. With no answer in 2 minutes you may force-release it.</p>
        ${off ? html`<${Reason} icon="circle-slash" testid="request-notes-off"
          text=${`No message: ${off}. ${holder || "The holder"} sees that you are waiting, not why.`} />`
        : html`<label class="field-label" for="lease-message">Message (optional)</label>
        <textarea id="lease-message" class="input lease-textarea" rows="3" maxlength=${NOTE_MAX}
          placeholder="Why you need it, and for how long" value=${f.message} data-autofocus
          onInput=${(e) => { f.message = e.target.value; }}></textarea>`}
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
    ${L.confirm ? html`<${ForceConfirm} key=${`c-${L.confirm.bid}`} />` : null}`;
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
  answers: L.answers, dismissedTaken: L.dismissedTaken,
}));
