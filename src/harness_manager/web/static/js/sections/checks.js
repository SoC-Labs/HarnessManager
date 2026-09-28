// Checks (lane HIL-GUI): the HIL runbooks, unattended, started from the app
// (docs/HIL_AUTO.md "In the app"; the service's run manager is services/hil_runs.py).
//
// - The plan is picked from the board (autoPlan: the SAME rule as checks.plans.auto_plan;
//   tests/web/test_hil_gui_browser.py holds the two to one table), and can be overridden.
// - The lease: yours -> the service heartbeats it until the run ends; free -> Start asks to
//   take it for the run (released at the end); someone else's -> Start is off and names them.
//   Nothing here ever forces a lease.
// - A run is an explicit action (QUIET-POLL's viewer rules do not gate it). This page reads
//   only GET /checks (the service's own state, never the board) and follows checks.* events.

import { call } from "../api.js";
import { clock } from "../format.js";
import { html, useEffect, useState } from "../lib.js";
import { boardState, changed, log, onBoardEvent, onBoardOpened, S, timed } from "../store.js";
import { Card, Chip, Icon, Reason, Spinner } from "../ui.js";
import { durationText, epochOf, leaseWho, loadHub } from "../week.js";

// --- the plan a board gets (checks.plans.auto_plan, in JS) ------------------------------------

function slotStates(card) {
  const slots = card && card.os_slots && card.os_slots.slots;
  if (!slots || typeof slots !== "object") return [];
  return Object.values(slots).filter((v) => v && typeof v === "object").map((v) => String(v.state || ""));
}

// identity: BoardInfo.identity (harness_impl); card: GET /card's `card`, or null (not read).
export function autoPlan(identity, card) {
  const impl = String((identity && identity.harness_impl) || "");
  if (impl !== "linux") {
    return { plan: "bare-metal", why: `${impl ? `the ${impl} harness` : "not the Linux harness"} (HIL_B0.md)` };
  }
  if (!card || typeof card !== "object") {
    return { plan: "linux-netboot", why: "the Linux harness; its user microSD was not read, so a blank card (netboot) is assumed" };
  }
  if (!card.present) return { plan: "linux-nocard", why: "the Linux harness with no user microSD card (Card-less mode)" };
  const states = slotStates(card);
  if (states.some((s) => s === "valid") || (!states.length && card.state === "valid")) {
    return { plan: "linux", why: "the Linux harness with a usable user microSD card (an OS slot is valid)" };
  }
  return { plan: "linux-netboot", why: "the Linux harness with a blank user microSD card (no valid OS slot: Netboot mode)" };
}

// --- state ---------------------------------------------------------------------------------------

const ACTIVE = new Set(["scheduled", "starting", "running", "stopping"]);

function cs(bid) {
  const b = boardState(bid);
  if (!b.checks) {
    b.checks = {
      status: null, error: null, loading: false,
      form: { plan: "auto", writes: "safe", until: "08:30", every: 30, when: "now", at: "18:00",
        take: false },
      busy: "", line: "", actionError: null, preview: null,
      report: null,           // {run, iteration, name, text, path, loading, error}
      open: null,             // the past run whose details are open
    };
  }
  return b.checks;
}

export function checksRun(bid) {
  const c = S.board[bid] && S.board[bid].checks;
  const run = c && c.status && c.status.run;
  return run && ACTIVE.has(run.state) ? run : null;
}

export async function loadChecks(bid) {
  const c = cs(bid);
  c.loading = true;
  changed();
  const r = await timed("checks", () => call("checks", { bid }));
  c.loading = false;
  c.error = r.error;
  if (!r.error) c.status = r.data.data;
  changed();
}

// checks.state: a run started, stopped or ended (read the list again); checks.progress: merge.
onBoardEvent((ev) => {
  const bid = ev.board_id;
  if (!bid || !ev.topic.startsWith("checks.")) return;
  const c = cs(bid);
  const d = ev.data || {};
  if (ev.topic === "checks.state") {
    loadChecks(bid);
    return;
  }
  const run = c.status && c.status.run;
  if (ev.topic === "checks.progress" && run && run.id === d.run) {
    for (const k of ["state", "iteration", "check", "counts", "totals", "next_at", "phase",
      "first_failure", "last_check"]) {
      if (d[k] !== undefined) run[k] = d[k];
    }
    if (d.kind === "iteration_end" || d.kind === "waiting") loadChecks(bid);
    changed();
  }
});

// The header banner needs to know about a run on a board this page opens (the service's own
// state: no board contact).
onBoardOpened((bid) => loadChecks(bid));

// --- the form ----------------------------------------------------------------------------------

function formBody(bid, extra = {}) {
  const f = cs(bid).form;
  const body = { plan: f.plan === "auto" ? autoOf(bid).plan : f.plan, writes: f.writes,
    until: f.until || "", interval_s: Number(f.every) * 60, ...extra };
  if (f.when === "at" && f.at) body.start_at = f.at;
  if (leaseWho(bid).state === "free" && f.take) body.take_lease = true;
  return body;
}

function autoOf(bid) {
  const b = boardState(bid);
  return autoPlan(b.info && b.info.identity, b.card);
}

// Why Start cannot run now ("" when it can): the lease rule first, never a silent grey button.
export function startWhy(bid) {
  const who = leaseWho(bid);
  const f = cs(bid).form;
  if (who.state === "none") return "This board is not behind a hub: the checks need its hub lease held here.";
  if (who.state === "unread") return "The lease has not been read yet.";
  if (who.state === "unknown") return `The lease could not be read${who.error ? ` (${who.error})` : ""}: read it again.`;
  if (who.state === "other") return `Leased to ${who.holder}: Start needs the lease free or held by this Harness Manager. Request the board, or wait.`;
  if (who.state === "elsewhere") return `Leased to ${who.holder} in another session or tool, not this Harness Manager: release it there first.`;
  if (who.state === "free" && !f.take) return "Nobody holds the lease: tick \"Take the lease for the run\".";
  if (f.when === "at" && !f.at) return "Give the start time.";
  return "";
}

function set(bid, key, value) {
  cs(bid).form[key] = value;
  cs(bid).preview = null;
  changed();
}

async function preview(bid) {
  const c = cs(bid);
  c.busy = "preview";
  changed();
  const r = await timed("checks preview", () => call("checksStart", { bid }, formBody(bid, { announce_only: true })));
  c.busy = "";
  c.actionError = r.error;
  c.line = r.line;
  if (!r.error) c.preview = r.data.data;
  changed();
}

async function start(bid) {
  const c = cs(bid);
  c.busy = "start";
  changed();
  const body = formBody(bid);
  const r = await timed(`checks start ${body.plan}`, () => call("checksStart", { bid }, body));
  c.busy = "";
  c.actionError = r.error;
  c.line = r.line;
  log(r.error ? "error" : "info", "checks", r.error ? `${r.line}  ${r.error.errName}: ${r.error.message}` : `${r.line}: run ${r.data.data.run.id}`, bid);
  if (!r.error) {
    c.status = { ...(c.status || {}), run: r.data.data.run };
    c.preview = null;
  }
  changed();
  loadChecks(bid);
}

async function stop(bid) {
  const c = cs(bid);
  c.busy = "stop";
  changed();
  const r = await timed("checks stop", () => call("checksStop", { bid }));
  c.busy = "";
  c.actionError = r.error;
  c.line = r.line;
  log(r.error ? "error" : "info", "checks", r.error ? `${r.line}  ${r.error.message}` : `${r.line}: finishing the check, then greybox`, bid);
  if (!r.error && c.status) c.status.run = r.data.data.run;
  changed();
}

function PlanPicker({ bid }) {
  const c = cs(bid);
  const auto = autoOf(bid);
  const plans = (c.status && c.status.plans) || [];
  return html`<div class="field checks-field">
    <label for="checks-plan">Plan</label>
    <select id="checks-plan" class="select grow" data-testid="checks-plan" value=${c.form.plan}
      onChange=${(e) => set(bid, "plan", e.target.value)}>
      <option value="auto">Auto: ${auto.plan}</option>
      ${plans.map((p) => html`<option key=${p.name} value=${p.name}>${p.name} (${p.runbook.replace("docs/", "")})</option>`)}
    </select>
  </div>
  <p class="checks-why" data-testid="checks-auto-why">${c.form.plan === "auto"
    ? html`<${Icon} name="info" cls="sm" /><span>Auto picked <b>${auto.plan}</b>: ${auto.why}.</span>`
    : html`<${Icon} name="triangle-alert" cls="sm" /><span>Chosen by hand. Auto would pick <b>${auto.plan}</b>.</span>`}</p>`;
}

function LeaseLine({ bid }) {
  const who = leaseWho(bid);
  const c = cs(bid);
  if (who.state === "here") {
    const left = who.lease && epochOf(who.lease.expires_at);
    return html`<${Reason} level="ok" icon="lock" testid="checks-lease"
      text=${`The lease is yours. The service heartbeats it until the run ends${left ? ` (now until ${clock(left)})` : ""}.`} />`;
  }
  if (who.state === "free") {
    return html`<label class=${`keep ${c.form.take ? "on" : ""}`} data-testid="checks-take-lease">
      <input type="checkbox" checked=${c.form.take} onChange=${(e) => set(bid, "take", e.target.checked)} />
      <${Icon} name="lock-open" /><span>Take the lease for the run
        <span class="sub">Nobody holds it now. The service takes it when the run starts, keeps it until the run ends, then releases it.</span></span></label>`;
  }
  if (who.state === "unread" || who.state === "unknown") {
    return html`<div class="row" data-testid="checks-lease"><${Reason} level="unk" text=${startWhy(bid)} />
      <button type="button" class="btn sm" onClick=${() => loadHub(bid)}><${Icon} name="refresh-cw" /> Read the lease</button></div>`;
  }
  return html`<${Reason} level="err" icon="lock" testid="checks-lease" text=${startWhy(bid)} />`;
}

// The lease line already says why Start is off in these states (said once, not twice).
const LEASE_SAYS = new Set(["none", "unread", "unknown", "other", "elsewhere"]);

function StartForm({ bid }) {
  const c = cs(bid);
  const f = c.form;
  const why = startWhy(bid);
  const defaults = (c.status && c.status.defaults) || {};
  return html`<div class="checks-form" data-testid="checks-form">
    <${PlanPicker} bid=${bid} />
    <div class="field checks-field"><label>Writes</label>
      <div class="seg" role="group" aria-label="Writes">
        ${[["none", "Read only", "Reads the board, the hub and HM's state: nothing changes"],
          ["safe", "Safe", "Also the swap-and-restore checks and the MCC read; greybox is put back"]].map(([v, label, title]) => html`
          <button type="button" key=${v} aria-pressed=${f.writes === v ? "true" : "false"} title=${title}
            data-testid=${`checks-writes-${v}`} onClick=${() => set(bid, "writes", v)}>${label}</button>`)}
      </div></div>
    <div class="field checks-field"><label for="checks-until">Run until</label>
      <input id="checks-until" type="time" class="input" data-testid="checks-until" value=${f.until}
        onInput=${(e) => set(bid, "until", e.target.value)} />
      <label for="checks-every">every</label>
      <select id="checks-every" class="select" data-testid="checks-every" value=${String(f.every)}
        onChange=${(e) => set(bid, "every", Number(e.target.value))}>
        ${[15, 20, 30, 60].map((m) => html`<option key=${m} value=${String(m)}>${m} min</option>`)}
      </select></div>
    <p class="checks-why">No check starts in the last ${defaults.margin_min || 10} min before then; the next ${f.until || "08:30"} is ${untilText(f.until)}.</p>
    <div class="field checks-field"><label>Start</label>
      <div class="seg" role="group" aria-label="Start">
        <button type="button" aria-pressed=${f.when === "now" ? "true" : "false"} data-testid="checks-when-now"
          onClick=${() => set(bid, "when", "now")}>Now</button>
        <button type="button" aria-pressed=${f.when === "at" ? "true" : "false"} data-testid="checks-when-at"
          onClick=${() => set(bid, "when", "at")}>At</button>
      </div>
      ${f.when === "at" ? html`<input type="time" class="input" data-testid="checks-at" value=${f.at}
        aria-label="Start at" onInput=${(e) => set(bid, "at", e.target.value)} />` : null}
    </div>
    <${LeaseLine} bid=${bid} />
    <div class="row mt-14">
      <button type="button" class="btn primary" data-action="checks-start" data-testid="checks-start"
        aria-disabled=${why ? "true" : undefined} disabled=${!!why || !!c.busy}
        aria-busy=${c.busy === "start" ? "true" : undefined} onClick=${() => start(bid)}>
        ${c.busy === "start" ? html`<${Spinner} />` : html`<${Icon} name="play" />`}
        ${f.when === "at" ? `Schedule for ${f.at || "?"}` : "Start"}</button>
      <button type="button" class="btn" data-action="checks-preview" data-testid="checks-preview"
        disabled=${!!c.busy} onClick=${() => preview(bid)}>
        ${c.busy === "preview" ? html`<${Spinner} />` : html`<${Icon} name="send" />`} Write the announcement</button>
    </div>
    ${why && !LEASE_SAYS.has(leaseWho(bid).state) ? html`<${Reason} icon="circle-slash" text=${why} testid="checks-start-why" />` : null}
    ${c.actionError ? html`<div class="result mt-8" data-testid="checks-error"><div><span class="rc err">${c.line}</span></div>
      <div><span class="errname">${c.actionError.errName}</span>  ${c.actionError.message}</div>
      ${c.actionError.hint ? html`<div class="hint">hint: ${c.actionError.hint}</div>` : null}</div>` : null}
  </div>`;
}

function untilText(hhmm) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm || "");
  if (!m) return "not set: the run ends with its last iteration";
  const now = new Date();
  const at = new Date(now);
  at.setHours(Number(m[1]), Number(m[2]), 0, 0);
  if (at <= now) at.setDate(at.getDate() + 1);
  const day = at.getDate() === now.getDate() ? "today" : "tomorrow";
  return `${day} ${hhmm}`;
}

// --- the live run --------------------------------------------------------------------------------

const VERDICT_LEVEL = { pass: "ok", fail: "err", stopped: "err", skipped: "", manual: "plain" };

function Counts({ counts, testid }) {
  const c = counts || {};
  return html`<span class="row checks-counts" data-testid=${testid}>
    ${["pass", "fail", "stopped", "skipped", "manual"].map((k) => html`<${Chip} key=${k}
      level=${(c[k] || 0) && VERDICT_LEVEL[k] ? VERDICT_LEVEL[k] : "plain"}>${c[k] || 0} ${k}<//>`)}
  </span>`;
}

function stateChip(run) {
  const r = run.result;
  if (r === "PASS") return html`<${Chip} level="ok" icon="circle-check" testid="checks-result">PASS<//>`;
  if (r === "FAIL") return html`<${Chip} level="err" icon="circle-x" testid="checks-result">FAIL<//>`;
  if (r === "STOPPED") return html`<${Chip} level="warn" icon="octagon-x" testid="checks-result">STOPPED<//>`;
  const level = run.state === "stopping" ? "warn" : run.state === "scheduled" ? "unk" : "accent";
  return html`<${Chip} level=${level} testid="checks-state">${ACTIVE.has(run.state) && run.state !== "scheduled"
    ? html`<${Spinner} />` : html`<${Icon} name="clock" />`}${run.state}<//>`;
}

function nowText(run) {
  if (run.state === "scheduled") return `Starts at ${clock(epochOf(run.start_at))}; nothing is sent before then.`;
  if (run.state === "stopping") return "Stopping: finishing the check, then greybox and REPORT.md.";
  if (run.phase === "end state") return "Putting greybox back and reading it back.";
  if (run.check) return html`Checking <b>${run.check.id}</b> ${run.check.title}`;
  if (run.last_check && run.phase === "checks") {
    return html`Last check <b>${run.last_check.id}</b> ${run.last_check.title}: ${run.last_check.verdict}`;
  }
  if (run.phase === "waiting" && run.next_at) {
    const at = epochOf(run.next_at);
    return `Waiting: the next iteration starts at ${clock(at)} (in ${durationText(Math.max(0, at - Date.now() / 1000))}).`;
  }
  return run.state === "starting" ? "Starting: the lease, then the first iteration." : "Running.";
}

function RunPanel({ bid, run }) {
  const c = cs(bid);
  const pct = run.repeat ? Math.min(100, Math.round((100 * (run.iterations || []).length) / run.repeat)) : 0;
  const ff = run.first_failure;
  const lease = run.lease || {};
  return html`<div class="checks-run" data-testid="checks-run" data-state=${run.state}>
    <div class="row">${stateChip(run)}
      <span class="mono">${run.plan}</span><span class="secondary">writes ${run.writes}</span>
      <span class="grow"></span>
      <button type="button" class="btn danger" data-action="checks-stop" data-testid="checks-stop"
        disabled=${run.stop_requested || !!c.busy} aria-busy=${c.busy === "stop" ? "true" : undefined}
        title="Finish the check it is on, put greybox back, write REPORT.md" onClick=${() => stop(bid)}>
        ${c.busy === "stop" ? html`<${Spinner} />` : html`<${Icon} name="square" />`}
        ${run.state === "scheduled" ? "Cancel" : run.stop_requested ? "Stopping..." : "Stop"}</button>
    </div>
    <p class="checks-now" data-testid="checks-now">${nowText(run)}</p>
    <div class="meter"><div style=${`width:${pct}%`}></div></div>
    <div class="meter-line"><span data-testid="checks-iteration">Iteration ${run.iteration || 0} of ${run.repeat}</span>
      <span>${run.until ? `until ${clock(epochOf(run.until))}` : "until the last iteration"}, every ${Math.round(run.interval_s / 60)} min</span></div>
    <dl class="kv mt-14">
      <dt>This iteration</dt><dd><${Counts} counts=${run.counts} testid="checks-counts" /></dd>
      <dt>The whole run</dt><dd><${Counts} counts=${run.totals} testid="checks-totals" /></dd>
      <dt>First failure</dt><dd data-testid="checks-first-failure">${ff ? html`<b>${ff.id}</b> ${ff.title || ""}
        <div class="sub">iteration ${ff.iteration}: ${ff.reason}</div>
        ${ff.hint ? html`<div class="sub">hint: ${ff.hint}</div>` : null}
        ${ff.evidence ? html`<div class="sub mono">${ff.evidence}</div>` : null}` : html`<span class="muted">none</span>`}</dd>
      <dt>Lease</dt><dd>${lease.taken ? "taken for this run: released when it ends"
        : lease.kept ? "held here: the service heartbeats it until the run ends" : lease.state || "?"}</dd>
      <dt>Evidence</dt><dd class="mono small-text">${run.evidence}</dd>
    </dl>
    ${(run.log || []).length ? html`<div class="result mt-14" role="log" data-testid="checks-log">
      ${(run.log || []).slice(-12).map((l, i) => html`<div key=${i}>${l}</div>`)}</div>` : null}
  </div>`;
}

// --- the announcement -----------------------------------------------------------------------------

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    ta.remove();
    return ok;
  }
}

function Announcement({ bid }) {
  const c = cs(bid);
  const [copied, setCopied] = useState("");
  const run = (c.status && (c.status.run || c.status.last)) || null;
  const text = (c.preview && c.preview.announce) || (run && run.announce) || "";
  const source = c.preview ? "what Start would write now" : run ? `run ${run.id}` : "";
  const onCopy = async () => {
    setCopied((await copyText(text)) ? "copied" : "select it and copy by hand");
    setTimeout(() => setCopied(""), 2000);
  };
  return html`<${Card} title="Announcement" icon="send" testid="checks-announce-card"
      sub="For the 17:30 announcement: start, planned end, plan, writes, what it changes and what it never does (ANNOUNCE.txt)."
      actions=${html`<button type="button" class="btn sm" data-action="checks-copy" data-testid="checks-copy"
        disabled=${!text} onClick=${onCopy}><${Icon} name=${copied === "copied" ? "check" : "copy"} />
        ${copied === "copied" ? "Copied" : "Copy"}</button>`}>
    ${text ? html`<textarea class="input checks-announce" readonly rows="12" data-testid="checks-announce"
        aria-label="The announcement" value=${text}></textarea>
      <p class="checks-why">${source}${copied && copied !== "copied" ? `: ${copied}` : ""}</p>`
      : html`<p class="muted">Write the announcement to see what the run will do before you start it.</p>`}
  <//>`;
}

// --- past runs and their reports ----------------------------------------------------------------------

async function openReport(bid, runId, iteration = null) {
  const c = cs(bid);
  c.open = runId;
  c.report = { run: runId, iteration, loading: true, error: null, text: "", path: "" };
  changed();
  const query = iteration ? { iteration: String(iteration) } : null;
  const r = await timed("checks report", () => call("checksReport", { bid, run: runId }, undefined, query));
  if (!c.report || c.report.run !== runId || c.report.iteration !== iteration) return;
  c.report = { run: runId, iteration, loading: false, error: r.error,
    text: r.error ? "" : r.data.data.text, path: r.error ? "" : r.data.data.path };
  changed();
}

function resultChip(result, state) {
  if (result === "PASS") return html`<${Chip} level="ok">PASS<//>`;
  if (result === "FAIL") return html`<${Chip} level="err">FAIL<//>`;
  if (result === "STOPPED") return html`<${Chip} level="warn">STOPPED<//>`;
  return html`<${Chip} level=${ACTIVE.has(state) ? "accent" : "plain"}>${state || "?"}<//>`;
}

function PastRuns({ bid }) {
  const c = cs(bid);
  const runs = (c.status && c.status.runs) || [];
  const refresh = html`<button type="button" class="btn ghost sm" onClick=${() => loadChecks(bid)}
    aria-busy=${c.loading ? "true" : undefined}>${c.loading ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`} Refresh</button>`;
  return html`<${Card} title="Past runs" icon="history" actions=${refresh} bodyCls="flush" testid="checks-runs"
      sub="Newest first. Open one for its REPORT.md and each iteration's.">
    ${runs.length ? html`<table class="table" data-testid="checks-runs-table">
      <colgroup><col style="width:34%" /><col style="width:24%" /><col style="width:18%" /><col /></colgroup>
      <thead><tr><th>Started</th><th>Plan</th><th>Result</th><th class="r">Iterations</th></tr></thead>
      <tbody>${runs.map((r) => html`<tr key=${r.id} class="pick" aria-selected=${c.open === r.id ? "true" : "false"}
          data-run=${r.id} tabindex="0" onClick=${() => openReport(bid, r.id)}
          onKeyDown=${(e) => { if (e.key === "Enter") openReport(bid, r.id); }}>
        <td>${(r.started_at || r.created_at || "").replace("T", " ").slice(0, 16)}<div class="sub mono">${r.id}</div></td>
        <td class="mono">${r.plan}<div class="sub">writes ${r.writes}</div></td>
        <td>${resultChip(r.result, r.state)}</td>
        <td class="r">${r.summary ? r.summary.iterations.length : r.iterations || 0}</td>
      </tr>`)}</tbody></table>`
      : html`<p class="muted pad-x">No runs on this board yet.</p>`}
    <${ReportView} bid=${bid} />
  <//>`;
}

function ReportView({ bid }) {
  const c = cs(bid);
  const rep = c.report;
  if (!rep) return null;
  const row = ((c.status && c.status.runs) || []).find((r) => r.id === rep.run) || null;
  const iters = (row && row.summary && row.summary.iterations) || [];
  return html`<div class="checks-report" data-testid="checks-report">
    ${iters.length > 1 ? html`<div class="row checks-iters" data-testid="checks-iterations">
      <button type="button" class="btn sm" aria-pressed=${rep.iteration ? "false" : "true"}
        onClick=${() => openReport(bid, rep.run)}>Whole run</button>
      ${iters.map((it) => html`<button type="button" key=${it.n} class="btn sm"
        aria-pressed=${rep.iteration === it.n ? "true" : "false"} data-iteration=${it.n}
        title=${it.first_failure ? `${it.first_failure.id}: ${it.first_failure.reason}` : ""}
        onClick=${() => openReport(bid, rep.run, it.n)}>
        <${Icon} name=${it.result === "PASS" ? "circle-check" : it.result === "FAIL" ? "circle-x" : "octagon-x"} cls=${it.result === "PASS" ? "i-ok" : "i-err"} />${it.n}</button>`)}
    </div>` : null}
    ${rep.loading ? html`<p class="muted"><${Spinner} /> Reading the report...</p>`
      : rep.error ? html`<${Reason} level="err" text=${`${rep.error.errName}: ${rep.error.message}`} />`
        : html`<div class="md" data-testid="checks-report-md"><${Markdown} text=${rep.text}
            onLink=${(href) => {
              const m = /iter-(\d+)\/REPORT\.md$/.exec(href);
              if (m) openReport(bid, rep.run, Number(m[1]));
            }} /></div>
          <p class="checks-why mono">${rep.path}</p>`}
  </div>`;
}

// --- a small, safe Markdown renderer (REPORT.md: headings, lists, tables, bold, code, links) --------
// Builds elements, never HTML strings: nothing in a report can inject markup.

function inline(text, onLink) {
  const out = [];
  const re = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\))/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[2] !== undefined) out.push(html`<strong>${m[2]}</strong>`);
    else if (m[3] !== undefined) out.push(html`<code>${m[3]}</code>`);
    else {
      const href = m[5];
      out.push(html`<a href="#" data-href=${href} onClick=${(e) => { e.preventDefault(); if (onLink) onLink(href); }}>${m[4]}</a>`);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function cells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((s) => s.trim());
}

export function Markdown({ text, onLink }) {
  const lines = String(text || "").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length;
      const body = inline(h[2], onLink);
      blocks.push(level === 1 ? html`<h3>${body}</h3>` : html`<h4>${body}</h4>`);
      i += 1;
      continue;
    }
    if (line.trim().startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) { rows.push(lines[i]); i += 1; }
      const head = cells(rows[0]);
      const body = rows.slice(1).filter((r) => !/^\s*\|[\s:|-]+\|\s*$/.test(r)).map(cells);
      blocks.push(html`<table class="table md-table"><thead><tr>${head.map((c) => html`<th>${inline(c, onLink)}</th>`)}</tr></thead>
        <tbody>${body.map((r) => html`<tr>${r.map((c) => html`<td>${inline(c, onLink)}</td>`)}</tr>`)}</tbody></table>`);
      continue;
    }
    if (/^\s*-\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*-\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*-\s+/, "")); i += 1; }
      blocks.push(html`<ul>${items.map((t) => html`<li>${inline(t, onLink)}</li>`)}</ul>`);
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*-\s|\s*\|)/.test(lines[i])) { para.push(lines[i]); i += 1; }
    blocks.push(html`<p>${inline(para.join(" "), onLink)}</p>`);
  }
  return html`${blocks}`;
}

// --- the section --------------------------------------------------------------------------------------

export function ChecksSection({ bid }) {
  const c = cs(bid);
  useEffect(() => {
    loadChecks(bid);
    // A backstop for a dropped event socket, only while a run is active (the page's own
    // state: GET /checks never touches the board).
    const timer = setInterval(() => {
      if (document.visibilityState === "visible" && checksRun(bid)) loadChecks(bid);
    }, 30000);
    return () => clearInterval(timer);
  }, [bid]);
  const run = checksRun(bid);
  const last = c.status && !run ? c.status.last : null;
  return html`<div class="grid two hil-checks" data-testid="checks">
    <div class="stack">
      <${Card} title=${run ? "The run" : "Start a run"} icon="list-checks" testid="checks-card"
          sub=${run ? "Runs in the Harness Manager service: close this window and it goes on."
            : "The HIL runbooks, unattended: the plan's automatic checks, every interval, until the time you give (docs/HIL_AUTO.md)."}>
        ${c.error ? html`<${Reason} level="err" text=${`checks: ${c.error.errName}: ${c.error.message}`} />` : null}
        ${!c.status && !c.error ? html`<p class="muted"><${Spinner} /> Reading...</p>` : null}
        ${run ? html`<${RunPanel} bid=${bid} run=${run} />` : c.status ? html`<${StartForm} bid=${bid} />` : null}
        ${last ? html`<div class="checks-last mt-14" data-testid="checks-last">
          <span class="secondary">Last run ${last.id}:</span> ${resultChip(last.result, last.state)}
          ${last.reason ? html`<span class="secondary small-text"> ${last.reason}</span>` : null}</div>` : null}
      <//>
      <${Announcement} bid=${bid} />
    </div>
    <${PastRuns} bid=${bid} />
  </div>`;
}

// The workspace banner while a run is active: what you do here may stop it.
export function ChecksBanner({ bid }) {
  const run = bid && checksRun(bid);
  if (!run) return null;
  return html`<div class="banner info" role="status" data-testid="checks-banner"><${Icon} name="list-checks" />
    <div class="grow"><strong>Checks ${run.state === "scheduled" ? `start at ${clock(epochOf(run.start_at))}` : "are running"} on this board</strong>${" "}
    (${run.plan}, writes ${run.writes}). An action of yours on the board may stop the run.</div></div>`;
}
