// Lease requests, force release and leaving the queue (docs/LEASE_REQUESTS.md, lane LR-D).
//
// Three people see this, from any section of the page (it renders above the header):
// - the REQUESTER: "Request board" (a small form, an optional message), then a bar with
//   the queue position, "the holder has been asked", the holder's 2:00 countdown, Leave
//   queue, and "Force release..." once the daemon says force is available. Force opens a
//   red confirm naming the holder; it is the only way to force.
// - the HOLDER: a prompt per incoming request: who wants the board and why, the
//   countdown to their force, Release now, or Keep for 5 / 15 / 30 / 60 min (+ message).
// - the VICTIM: a banner "<board> was force-released by <by> at <time>: <reason>" that
//   stays until dismissed (across reloads) and is written to Activity.
//
// Every countdown runs to a time the daemon read from the hub's note (deadline_at, an
// answer's at + minutes), never to a timer this page started.

import { gateReason, interlock, panelState, runAction, runJob } from "./actions.js";
import { call, routeMissing } from "./api.js";
import { boardName, clock, hostOf } from "./format.js";
import { html, useLayoutEffect, useRef, useState } from "./lib.js";
import { changed, log, onBoardEvent, S } from "./store.js";
import { epochOf, loadHub, onHubLoaded, scheduleHub, week } from "./week.js";
import { Icon, Reason, ResultBlock, Spinner } from "./ui.js";

export const KEEP_MINUTES = [5, 15, 30, 60];
const NOTE_MAX = 500;                   // characters; a note on the hub is at most 4 KiB

// --- small helpers -----------------------------------------------------------------------------

function hubOf(bid) {
  const b = S.board[bid];
  return (b && b.week && b.week.hub) || null;
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

function sessionStore() { try { return window.sessionStorage; } catch (e) { return null; } }
function localStore() { try { return window.localStorage; } catch (e) { return null; } }

// --- page state --------------------------------------------------------------------------------

const DISMISSED_KEY = "harness_manager.lease_taken_dismissed";   // {bid: at}, survives reloads
const ANSWERS_KEY = "harness_manager.lease_answers";             // {bid: {id: answer}}, per tab

const L = {
  form: null,                // {bid, message, trigger}: the "Request board" form
  confirm: null,             // {bid, trigger}: the force confirm
  leaving: new Set(),        // boards whose request this page withdrew
  answers: recall(sessionStore(), ANSWERS_KEY),
  dismissedTaken: recall(localStore(), DISMISSED_KEY),
  takenLogged: new Set(),
  resultDismissed: {},       // bid -> the startedAt of the request result the user closed
  answerDismissed: new Set(),
  zeroSeen: new Set(),       // request ids whose countdown this page saw reach zero
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
  answered: () => "the holder answered",
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
  const message = f.message.trim();
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

function forceSpec(bid) {
  const name = leaseBoardName(bid);
  const hub = hubOf(bid);
  const holder = (hub && hub.lease && hub.lease.holder) || "the holder";
  return {
    key: "lease_force", label: "Force release", busyLabel: "Force releasing...", budgetS: 120,
    command: `lease force ${cliTarget(bid)} --yes`,
    run: (ctx) => runJob("leaseForce", { bid }, { confirm: true },
      (d) => ctx.progress(d.phase || "revoking", d.phase), "lease_force"),
    render: (r) => {
      const at = r && r.lease && epochOf(r.lease.expires_at);
      return [{ kind: "ok", text: `${holder} was force-released; ${name} is yours${at ? ` until ${clock(at)}` : ""}. Their session is told who took it.` }];
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
  L.confirm = { bid, trigger };
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
  const spec = forceSpec(bid);
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
      ${answer && answer.answer === "keep" ? html`<div class="lease-answer" data-testid="req-answer">
        <${Icon} name="clock" cls="sm" /><span><strong>${holder || "The holder"} is keeping it for ${answer.minutes} min</strong>${answer.message ? html`: ${quoted(answer.message)}` : null}
        ${keepLeft !== null ? (keepLeft > 0
          ? html`<span class="lease-fact" data-testid="req-keep-left">${minutesLeft(keepLeft)} min left${keepEnd ? ` (until ${clock(keepEnd)})` : ""}</span>`
          : html`<span class="lease-fact" data-testid="req-keep-left">their ${answer.minutes} min ran out</span>`) : null}</span>
      </div>` : null}
      ${req && req.message ? html`<div class="secondary small">Your message: ${quoted(req.message)}</div>` : null}
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
  remember(sessionStore(), ANSWERS_KEY, L.answers);
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
        at: Date.now() / 1000, by: note.by, board: target });
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

function HolderPrompt({ bid, note }) {
  const [message, setMessage] = useState("");
  const p = panelState(bid, `lease_respond:${note.id}`);
  const name = leaseBoardName(bid);
  const left = secondsTo(note.deadline_at);
  const busy = !!p.running;
  return html`<div class="banner lease-prompt" role="alert" data-testid="lease-wanted" data-request=${note.id} data-board=${bid}>
    <${Icon} name="triangle-alert" />
    <div class="grow">
      <div class="lease-prompt-title" data-testid="wanted-title"><strong>${note.by}</strong> wants <strong>${name}</strong>${note.message
        ? html`: ${quoted(note.message)}` : html`. <span class="muted">(no message)</span>`}</div>
      <div class="secondary small">
        ${left === null ? "Answer now, or they may force-release it."
          : left > 0 ? html`Answer within <span class="lease-clock" data-testid="wanted-countdown" data-left=${Math.ceil(left)}><${Icon} name="timer" cls="sm" />${mmss(left)}</span>, or they may force-release it: anything you run on the board is interrupted then.`
          : html`<span class="lease-clock due" data-testid="wanted-countdown" data-left="0"><${Icon} name="timer" cls="sm" />0:00</span> Their 2 minutes are up: they may force-release it now.`}
      </div>
      <div class="lease-actions">
        <input class="input lease-msg" type="text" maxlength=${NOTE_MAX} placeholder="Message (optional)"
          aria-label=${`Message to ${note.by} (optional)`} data-testid="wanted-message" value=${message}
          onInput=${(e) => setMessage(e.target.value)} disabled=${busy} />
        <button type="button" class="btn sm primary" data-action="respond_release" disabled=${busy}
          onClick=${() => respond(bid, note, "release", 0, message.trim())}><${Icon} name="lock-open" /> Release now</button>
        <span class="keep-group" role="group" aria-label="Keep it for">
          <span class="keep-label">Keep for</span>
          ${KEEP_MINUTES.map((m) => html`<button type="button" key=${m} class="btn sm" data-action=${`respond_keep_${m}`}
            disabled=${busy} onClick=${() => respond(bid, note, "keep", m, message.trim())}>${m} min</button>`)}
        </span>
        ${busy ? html`<span class="muted small"><${Spinner} /> answering</span>` : null}
      </div>
      ${p.lines.length ? html`<${ResultBlock} lines=${p.lines} panel=${p} testid="result-lease_respond" />` : null}
    </div>
  </div>`;
}

function AnsweredLine({ bid, id, a }) {
  const p = panelState(bid, `lease_respond:${id}`);
  const until = a.answer === "keep" ? a.at + 60 * a.minutes : null;
  const close = () => { L.answerDismissed.add(`${bid}|${id}`); changed(); };
  return html`<div class="banner lease-bar ok" role="status" data-testid="lease-answered" data-request=${id}>
    <${Icon} name="circle-check" />
    <div class="grow">
      <strong>${a.answer === "release"
        ? `You released ${a.board} to ${a.by} at ${clock(a.at)}.`
        : `You answered ${a.by}: keep ${a.board} for ${a.minutes} min${a.message ? "" : "."}`}</strong>${a.answer === "keep" && a.message
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
  for (const note of incoming) {
    ids.add(note.id);
    const a = note.answer || answerOf(bid, note.id);      // `answer` if the daemon adds it
    if (!a) out.push(html`<${HolderPrompt} key=${`w-${bid}-${note.id}`} bid=${bid} note=${note} />`);
  }
  // What this page answered stays in view until closed, even once the request has gone.
  for (const [id, a] of Object.entries(L.answers[bid] || {})) {
    if (L.answerDismissed.has(`${bid}|${id}`)) continue;
    if (!ids.has(id) && Date.now() / 1000 - a.at > 3600) continue;
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

function dismissTaken(bid, t) {
  L.dismissedTaken[bid] = takenKey(t);
  remember(localStore(), DISMISSED_KEY, L.dismissedTaken);
  changed();
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
  return html`<div class="modal-back" onClick=${(e) => { if (e.target === e.currentTarget) closeForm(); }}>
    <form class="modal small" role="dialog" aria-modal="true" aria-labelledby="lease-form-title" ref=${ref}
      data-testid="lease-request-form" onSubmit=${(e) => { e.preventDefault(); sendRequest(); }}>
      <div class="modal-head"><${Icon} name="send" /><h2 class="card-title" id="lease-form-title">Request ${name}</h2></div>
      <div class="modal-pad">
        <p class="secondary">${holder ? html`<strong>${holder}</strong> holds it.` : "Someone else holds it."} You join the
          queue and they are asked to give it up. With no answer in 2 minutes you may force-release it.</p>
        <label class="field-label" for="lease-message">Message (optional)</label>
        <textarea id="lease-message" class="input lease-textarea" rows="3" maxlength=${NOTE_MAX}
          placeholder="Why you need it, and for how long" value=${f.message} data-autofocus
          onInput=${(e) => { f.message = e.target.value; }}></textarea>
      </div>
      <div class="modal-foot">
        <button type="button" class="btn" data-action="lease_request_cancel" onClick=${closeForm}>Cancel</button>
        <button type="submit" class="btn primary" data-action="lease_request"><${Icon} name="send" /> Send request</button>
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
  return html`<div class="modal-back" onClick=${(e) => { if (e.target === e.currentTarget) closeConfirm(); }}>
    <div class="modal small danger" role="alertdialog" aria-modal="true" aria-labelledby="force-title"
      aria-describedby="force-what" ref=${ref} data-testid="force-confirm">
      <div class="modal-head"><${Icon} name="zap" /><h2 class="card-title" id="force-title">Force release ${name}?</h2></div>
      <div class="modal-pad">
        <p id="force-what" class="force-what" data-testid="force-what">This kicks <strong>${holder}</strong> off <strong>${name}</strong> now;
          anything they are running is interrupted.</p>
        <p class="secondary small">The hub records who did it and why${made ? ` (no answer to your request made at ${clock(made)})` : ""},
          and ${holder}'s session is told who took the board.</p>
        ${why ? html`<${Reason} level="err" text=${`Not available now: ${why}`} testid="force-confirm-why" />` : null}
      </div>
      <div class="modal-foot">
        <button type="button" class="btn" data-action="force_cancel" onClick=${closeConfirm} data-autofocus>Cancel</button>
        <button type="button" class="btn danger-solid" data-action="force_confirm"
          aria-disabled=${why ? "true" : undefined} onClick=${confirmForce}><${Icon} name="zap" /> Force</button>
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
  if (ev.topic === "lease.taken") {
    if (w.hub) w.hub = { ...w.hub, taken: { ...d } };
    noteTaken(bid, d);
  }
  if (["lease.wanted", "lease.answered", "lease.force_available", "lease.taken", "lease.left"].includes(ev.topic)) {
    scheduleHub(bid, 50);
  }
});

onHubLoaded((bid, hub) => {
  if (hub && hub.taken) noteTaken(bid, hub.taken);
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
      const key = `${bid}|${req.id}|${req.answer ? "keep" : "wait"}`;
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
