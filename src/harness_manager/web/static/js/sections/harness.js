// The board's Harness versions card (lane H9; docs/design/HARNESS_DISTRIBUTION.md §5, §8.3,
// docs/API.md "Harness versions"): every signed release of the board pack's harness
// catalogue, each with a verdict for THIS board, what installing it changes, and Install,
// Pin, History and Roll back.
//
// - The list is the daemon's cached catalogue (GET /harness/catalog); Refresh rebuilds it
//   (a job: it fetches and verifies each channel and plans every release for the board).
// - Install shows the plan the daemon computed (GET /harness/releases/{v}?board_id=) and
//   binds to its fingerprint. A re-key needs the typed "REKEY 0x…" phrase; nothing implies
//   it. Behind a hub the lease holder installs: the daemon's 409 HELD names who holds it.
// - U11: harness versions. U9/U10 (lane HUB-SD): a board behind the hub installs "via the
//   hub" (fpgahub writes nanosoc.bit; a client timeout there is expected), with a typed
//   phrase naming the board, its lease holder and queue, and auto-revert of a board that
//   stays dark, armed by default. U8 (A/B by pointer) is built but off until its board check.

import { panelState, runJob } from "../actions.js";
import { call, toApiError } from "../api.js";
import { bytesText, clock } from "../format.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed, onBoardEvent, timed } from "../store.js";
import { ActionRow, ArmBox, Card, Chip, Icon, Reason, ResultBlock, Spinner } from "../ui.js";

export const DOOR_TEXT = "needs Debug USB here, or a hub that can write its SD";
export const NOT_YET = "Not yet: the A/B config SD (U8) waits for its board check. Today a local "
  + "install rewrites the config SD in place, with a backup first; an install via the hub writes "
  + "nanosoc.bit only, keeps the previous one, and reverts a board that stays dark.";
// HUB-SD: what each phase of an install via the hub is doing (update.progress phases).
export const HUB_PHASE = [
  ["backup:", "keeping the previous nanosoc.bit (signed, from the cache) as the backup"],
  ["sd:uploading", "uploading nanosoc.bit to the hub"],
  ["sd:writing", "writing the SD on the hub: a client timeout here is expected (the hub keeps writing)"],
  ["sd:verifying", "verifying: waiting for the hub's record of our sha256"],
  ["sd:verified", "verified by the hub's own record"],
  ["reboot:", "rebooting by the MCC on the hub (paced)"],
  ["confirm", "confirming the identity the board reports"],
  ["revert:", "AUTO-REVERT: writing the previous nanosoc.bit back through the hub"],
  ["revert-reboot:", "AUTO-REVERT: rebooting into the previous base"],
];

const VERDICT = {
  fits: { level: "ok", icon: "circle-check", text: "fits" },
  "re-key": { level: "warn", icon: "triangle-alert", text: "re-key" },
  "needs-door": { level: "unk", icon: "usb", text: "needs Debug USB or hub" },
  incompatible: { level: "err", icon: "circle-x", text: "incompatible" },
};
const MARK = {
  running: { level: "accent", icon: "activity", title: "the board reports this release's identity" },
  installed: { level: "ok", icon: "check", title: "this Harness Manager's last install on the board" },
  written: { level: "warn", icon: "triangle-alert", title: "written to the SD, but the board does not run it" },
  pinned: { level: "held", icon: "lock", title: "the board is pinned to this release" },
  current: { level: "plain", icon: "", title: "the channel's current release" },
  offered: { level: "plain", icon: "download", title: "what an install with no version gives" },
  "past-pin": { level: "unk", icon: "", title: "newer than the pin: not offered" },
};

// --- per-board state -----------------------------------------------------------------------------

export function hv(bid) {
  const b = boardState(bid);
  if (!b.harness) {
    b.harness = {
      catalog: null, error: null, empty: false, unavailable: "", loading: false,
      all: false,               // refresh stable only, or stable + beta + dev
      open: {},                 // version -> "What changes" open
      pick: "", pickRow: null, done: "", detail: null, detailError: null, detailLoading: false,
      typed: "",                // the typed re-key phrase
      boardTyped: "",           // HUB-SD: the typed board phrase of an install via the hub
      autoRevert: null,         // HUB-SD (U10): null = the plan's default (armed)
      history: null, historyError: null, historyOpen: false,
      rollback: null, rollbackError: null,   // {plan, to}: the plan a rollback would run
      stale: "",                // a harness.* event from elsewhere changed the catalogue
    };
  }
  return b.harness;
}

export async function loadCatalog(bid) {
  const h = hv(bid);
  h.loading = true;
  changed();
  const r = await timed("harness list", () => call("harnessCatalog", {}, undefined, { board_id: bid }));
  h.loading = false;
  if (r.error) {
    if (r.error.errName === "REFUSED") {          // nothing cached yet: "refresh it first"
      h.empty = true;
      h.catalog = null;
    } else if (r.error.errName === "UNAVAILABLE" || r.error.status === 404) {
      h.unavailable = r.error.reason || r.error.message;
    } else if (!(r.error.errName === "HELD")) {
      h.error = r.error;
    }
  } else {
    setCatalog(bid, r.data.data);
  }
  changed();
}

function setCatalog(bid, data) {
  const h = hv(bid);
  h.catalog = data;
  h.empty = false;
  h.error = null;
  h.unavailable = "";
  h.stale = "";
  if (h.pick && !(data.releases || []).some((r) => r.version === h.pick)) closePick(bid);
}

function refreshBody(bid) {
  return { board_id: bid, ...(hv(bid).all ? { all: true } : {}) };
}

// Rebuild the list after an install, a pin or a rollback (a short board job).
async function refreshQuietly(bid) {
  try {
    const out = await runJob("harnessRefresh", { bid }, refreshBody(bid), null, "harness_refresh");
    if (out && out.releases) setCatalog(bid, out);
  } catch (e) {
    hv(bid).error = toApiError(e);
  }
  changed();
}

export async function pickRelease(bid, version) {
  const h = hv(bid);
  h.pick = version;
  h.pickRow = ((h.catalog && h.catalog.releases) || []).find((r) => r.version === version) || null;
  h.done = "";
  h.detail = null;
  h.detailError = null;
  h.detailLoading = true;
  h.typed = "";
  h.boardTyped = "";
  h.autoRevert = null;
  h.rollback = null;
  panelState(bid, "harness_install").lines = [];
  changed();
  const r = await timed(`harness show ${version}`, () => call("harnessRelease", { version }, undefined, { board_id: bid }));
  if (h.pick !== version) return;
  h.detailLoading = false;
  if (r.error) h.detailError = r.error;
  else h.detail = r.data.data;
  changed();
}

function closePick(bid) {
  const h = hv(bid);
  h.pick = "";
  h.detail = null;
  h.detailError = null;
  h.typed = "";
  changed();
}

async function loadHistory(bid) {
  const h = hv(bid);
  const r = await timed("harness history", () => call("harnessHistory", { bid }, undefined, { limit: 20 }));
  h.historyError = r.error;
  if (!r.error) h.history = r.data.data;
  changed();
}

onBoardEvent((ev) => {
  const bid = ev.board_id;
  if (!bid || !ev.topic.startsWith("harness.")) return;
  const h = boardState(bid).harness;
  if (!h) return;
  if (ev.topic === "harness.pinned" || ev.topic === "harness.installed") {
    // this page refreshes after its own; one from the CLI or another page makes the list stale
    const p = panelState(bid, "harness_install");
    const q = panelState(bid, "harness_pin");
    if (!p.running && !q.running) h.stale = ev.topic === "harness.pinned" ? "the pin changed" : "a harness was installed";
    if (h.historyOpen) loadHistory(bid);
  }
});

// --- pieces ----------------------------------------------------------------------------------------

function VerdictChip({ row }) {
  const v = VERDICT[row.verdict];
  if (!v) return html`<span class="muted small">no verdict</span>`;
  return html`<${Chip} level=${v.level} icon=${v.icon} testid="verdict" title=${row.why}>${v.text}<//>`;
}

function Marks({ row }) {
  return html`<span class="marks" data-testid="marks">${(row.marks || []).map((m) => {
    const look = MARK[m] || { level: "plain", icon: "", title: m };
    return html`<${Chip} key=${m} level=${look.level} icon=${look.icon} title=${look.title} cls="mark">${m}<//>`;
  })}</span>`;
}

function whyText(row) {
  if (row.verdict === "needs-door") return `${DOOR_TEXT}: ${row.why}`;
  return row.why;
}

function ViaChip({ row }) {
  if (row.via !== "hub") return null;
  return html`<${Chip} level="accent" icon="server" testid="via-hub"
    title="fpgahub on the lab hub writes the config SD; the board is rebooted by the MCC on the hub (paced)">via the hub<//>`;
}

// Why Install cannot open for this row ("" when it can).
function installBlock(row) {
  if ((row.marks || []).includes("running")) return "the board runs it";
  if (row.verdict === "needs-door") return DOOR_TEXT;
  if (row.verdict === "incompatible") return `Cannot: ${row.why}`;
  if (!row.verdict) return "no verdict without a board";
  return "";
}

function Changes({ changes }) {
  if (!changes) return null;
  return html`<ul class="changes" data-testid="changes">${(changes.summary || []).map((l) => html`<li key=${l}>${l}</li>`)}</ul>`;
}

function Row({ bid, row, h, pinBusy }) {
  const open = !!h.open[row.version];
  const block = installBlock(row);
  const pinned = (row.marks || []).includes("pinned");
  const toggle = () => { h.open[row.version] = !open; changed(); };
  return html`<li class=${`hrow ${h.pick === row.version ? "picked" : ""}`} data-release=${row.version}
      data-verdict=${row.verdict || "none"}>
    <div class="hrow-main">
      <div class="hrow-id">
        <span class="mono hver">${row.version}</span>
        <span class="tags">${(row.channels || []).map((c) => html`<span class="tag" key=${c}>${c}</span>`)}</span>
        <${Marks} row=${row} />
      </div>
      <div class="hrow-verdict"><${VerdictChip} row=${row} /><${ViaChip} row=${row} />
        <span class="secondary small hwhy" data-testid="why">${whyText(row)}</span></div>
      <div class="hrow-facts secondary small">
        <span class="mono">${row.static_id}</span>${` · ${row.impl || "?"} · fw `}<span class="mono">${(row.fw_sha || "?").slice(0, 8)}</span>${
        ` · ${bytesText(row.size)}`}${row.cached ? html`${" · "}<span class="i-ok" title="in the download cache">cached</span>` : ""}${
        row.released_at ? ` · released ${String(row.released_at).slice(0, 10)}` : ""}
      </div>
      <div class="hrow-actions">
        <button type="button" class="btn ghost sm" data-action="changes" aria-expanded=${open ? "true" : "false"}
          onClick=${toggle}><${Icon} name="chevron-right" cls=${`sm chev ${open ? "open" : ""}`} />What changes</button>
        <button type="button" class="btn sm" data-action="pin" aria-disabled=${pinBusy ? "true" : undefined}
          title=${pinned ? "Stop pinning the board to this release" : "Pin the board: nothing newer is offered"}
          onClick=${() => { if (!pinBusy) (pinned ? unpin(bid) : pin(bid, row.version)); }}>
          <${Icon} name=${pinned ? "lock-open" : "lock"} />${pinned ? "Unpin" : "Pin"}</button>
        <button type="button" class="btn sm primary" data-action="install" aria-disabled=${block ? "true" : undefined}
          title=${block || `Plan installing harness ${row.version} on this board`}
          onClick=${() => { if (!block) pickRelease(bid, row.version); }}><${Icon} name="upload" /> Install…</button>
      </div>
    </div>
    ${open ? html`<div class="hrow-more">
      <${Changes} changes=${row.changes} />
      ${row.notes ? html`<p class="secondary small" data-testid="notes">${row.notes}</p>` : null}
      ${(row.warnings || []).map((w) => html`<${Reason} key=${w} level="warn" text=${w} />`)}
    </div>` : null}
  </li>`;
}

// --- pin, install, rollback -------------------------------------------------------------------------

async function pin(bid, version) {
  const p = panelState(bid, "harness_pin");
  p.running = "pin";
  changed();
  const r = await timed(`harness pin ${version}`, () => call("harnessPin", { bid }, { version }));
  p.running = null;
  p.lines = r.error ? [{ kind: "rc", command: `harness pin ${version}`, rc: r.error.code, secs: 0, level: "err" },
    { kind: "err", name: r.error.errName, text: r.error.message }] : [];
  changed();
  if (!r.error) await refreshQuietly(bid);
}

async function unpin(bid) {
  const p = panelState(bid, "harness_pin");
  p.running = "unpin";
  changed();
  const r = await timed("harness pin --clear", () => call("harnessUnpin", { bid }));
  p.running = null;
  p.lines = r.error ? [{ kind: "rc", command: "harness pin --clear", rc: r.error.code, secs: 0, level: "err" },
    { kind: "err", name: r.error.errName, text: r.error.message }] : [];
  changed();
  if (!r.error) await refreshQuietly(bid);
}

function outcomeLines(o) {
  if (!o || typeof o !== "object") return [{ kind: "out", text: "done" }];
  const good = o.result === "installed" || o.result === "restored" || o.result === "stored";
  // HUB-SD (U10): a board left dark says so as loudly as the card can
  const loud = o.result === "dark" || o.result === "auto-reverted" || o.result === "auto-revert-failed";
  const out = [{ kind: good ? "ok" : loud ? "err" : "warnline", text: `${o.result}: ${o.detail || ""}` }];
  const bad = (o.checks || []).filter((c) => c.check !== "ok");
  for (const c of bad) out.push({ kind: c.check === "unchecked" ? "hint" : "warnline", text: `${c.name}: ${String(c.check).toUpperCase()} (${c.detail})` });
  if (o.restore_hint) out.push({ kind: "hint", text: o.restore_hint });
  return out;
}

function progressText(d) {
  // SLOT-TIMING: a long card job says itself ("writing slot B: 12.3 MB / 29 MB, ~6 min left")
  if (d.text) return d.text;
  const phase = d.phase || "working";
  const hub = HUB_PHASE.find(([p]) => phase === p || (p.endsWith(":") && phase.startsWith(p)));
  const name = hub ? hub[1] : phase;
  return `${name}${d.total > 1 ? `: ${Math.floor((d.done * 100) / d.total)}%` : ""}`;
}

// HUB-SD: the typed phrase of an install via the hub (it names the board, its lease holder
// and the queue) and the auto-revert switch (armed by default: U10).
function DoorConsent({ bid, h, plan, id }) {
  if (!plan || plan.via !== "hub") return null;
  const hub = plan.hub || {};
  if (h.autoRevert === undefined || h.autoRevert === null) h.autoRevert = !!plan.auto_revert;
  return html`<div class="stack gap-8" data-testid="harness-door">
    <${Reason} level="warn" icon="server" testid="harness-door-text" text=${hub.consent_text || plan.board_phrase} />
    <div class="field rekey"><label for=${`${id}-${bid}`}>Board</label>
      <input class="input mono grow" id=${`${id}-${bid}`} placeholder=${`type ${plan.board_phrase}`} autocomplete="off" spellcheck="false"
        value=${h.boardTyped || ""} onInput=${(e) => { h.boardTyped = e.target.value; changed(); }} data-testid="harness-board-phrase" /></div>
    <label class="check-inline" title="If the board answers neither ping nor version after the REBOOT, write the previous nanosoc.bit back through the hub and REBOOT again">
      <input type="checkbox" data-testid="harness-auto-revert" checked=${!!h.autoRevert} disabled=${!plan.auto_revert}
        onChange=${(e) => { h.autoRevert = e.target.checked; changed(); }} />
      auto-revert if the board stays dark${plan.auto_revert ? "" : " (no backup: unavailable)"}</label>
  </div>`;
}

function doorBody(h, plan) {
  if (!plan || plan.via !== "hub") return {};
  return { via: "hub", board_phrase: (h.boardTyped || "").trim(), auto_revert: !!h.autoRevert };
}

function doorGuard(h, plan) {
  if (plan && plan.via === "hub" && (h.boardTyped || "").trim() !== plan.board_phrase) {
    return `via the hub: type exactly ${plan.board_phrase}`;
  }
  return "";
}

function PlanSteps({ plan }) {
  if (!plan || !(plan.steps || []).length) return null;
  return html`<ol class="plan-steps" data-testid="harness-steps">${plan.steps.map((s, i) => html`<li key=${i}>
    <span class="mono">${s.action}</span><span class="secondary">${s.detail}</span></li>`)}</ol>`;
}

function LeaseLine({ lease }) {
  if (!lease || !lease.required) return null;
  if (lease.mine) return html`<${Reason} level="ok" testid="harness-lease" text=${`You hold this board's hub lease${lease.target ? ` (${lease.target})` : ""}: installs are yours to run.`} />`;
  return html`<${Reason} level="warn" icon="lock" testid="harness-lease"
    text=${`Installs need this board's hub lease: ${lease.reason || `${lease.holder || "someone else"} holds it`}. The daemon refuses anyone else and names the holder.`} />`;
}

function InstallPanel({ bid, h }) {
  const d = h.detail;
  // the row as it was when the plan was asked for (the list refreshes after an install)
  const row = h.pickRow || d || {};
  const done = h.done === h.pick;
  const plan = d && d.plan;
  const p = panelState(bid, "harness_install");
  const phrase = (plan && plan.consent_phrase) || row.consent_phrase || "";
  const rekey = !!((plan && plan.rekey) || row.rekey);
  const board = (h.catalog && h.catalog.board) || {};
  const install = {
    key: "harness_install", label: `Install harness ${h.pick}`, busyLabel: "Installing...", budgetS: 900,
    command: `harness install ${bid} ${h.pick}${rekey ? ` --consent "${h.typed.trim()}"` : ""}`,
    run: (ctx) => runJob("harnessInstall", { bid }, { fingerprint: plan.fingerprint, version: h.pick,
      ...(rekey ? { rekey_phrase: h.typed.trim() } : {}), ...doorBody(h, plan) },
    (x) => ctx.progress(progressText(x), String(x.phase || "").split(":")[0]), "harness_install"),
    render: outcomeLines,
    renderError: (e) => (e.data && e.data.outcome ? outcomeLines(e.data.outcome) : []),
    onDone: (ok) => { h.typed = ""; h.boardTyped = ""; h.autoRevert = null; if (ok) { h.done = h.pick; refreshQuietly(bid); } changed(); },
  };
  const guard = () => {
    if (done) return "installed: this plan is spent";
    if (!plan) return h.detailLoading ? "reading the plan" : "no plan for this board";
    if ((plan.blockers || []).length) return `blocked: ${plan.blockers[0]}`;
    if (plan.up_to_date) return "the board already runs this release";
    if (rekey && h.typed.trim() !== phrase) return `a re-key: type exactly ${phrase}`;
    return doorGuard(h, plan);
  };
  const viaHub = !!(plan && plan.via === "hub");
  return html`<div class="card inset" data-testid="harness-install" data-release=${h.pick}>
    <div class="card-head"><h3 class="card-title"><${Icon} name="upload" />Install harness ${h.pick}</h3>
      <span class="spacer"></span>
      <button type="button" class="btn ghost sm icon-only" aria-label="Close" data-action="install-close"
        onClick=${() => closePick(bid)}><${Icon} name="x" /></button></div>
    <div class="card-body actions">
      ${h.detailLoading ? html`<p class="muted"><${Spinner} /> Planning it for this board...</p>` : null}
      ${h.detailError ? html`<${Reason} level="err" text=${`${h.detailError.errName}: ${h.detailError.message}`} />` : null}
      ${plan ? html`<div class="row">
          <${VerdictChip} row=${row} /><${Chip} icon="layers">${plan.mode}<//>
          <span class="secondary small">from the <b>${plan.channel}</b> channel #${plan.serial}; running shell <span class="mono">${(plan.running || {}).shell_id || "?"}</span></span></div>
        <${Changes} changes=${row.changes} />
        ${(plan.blockers || []).map((t) => html`<${Reason} key=${t} level="err" text=${t} testid="harness-blocker" />`)}
        ${(plan.warnings || []).map((t) => html`<${Reason} key=${t} level="warn" text=${t} />`)}
        <${PlanSteps} plan=${plan} />
        <${LeaseLine} lease=${board.lease} />
        ${done ? html`<${Reason} level="ok" testid="harness-installed"
          text=${`Installed. The list above is read again; this plan is spent (pick a release to plan the next install).`} />` : null}
        ${done ? null : html`${rekey ? html`<div class="field rekey"><label for=${`hv-rk-${bid}`}>Consent</label>
          <input class="input mono grow" id=${`hv-rk-${bid}`} placeholder=${`type ${phrase}`} autocomplete="off" spellcheck="false"
            value=${h.typed} onInput=${(e) => { h.typed = e.target.value; changed(); }} data-testid="harness-rekey" /></div>
          <p class="secondary small">A re-key changes the static: every overlay and DUT RM keyed to ${(plan.running || {}).shell_id || "the running static"} stops loading.</p>` : null}
        <${DoorConsent} bid=${bid} h=${h} plan=${plan} id="hv-bp" />
        <${ArmBox} bid=${bid} armKey="harness_install" testid="arm-harness"
          text=${viaHub ? "Arm: I understand the hub writes this board's config SD (the previous nanosoc.bit is kept) and the board is rebooted by the MCC on the hub (paced)."
            : "Arm: I understand this writes the board's config SD (after a backup) and reboots the board."} />
        <${ActionRow} bid=${bid} panel="harness_install" spec=${install} variant="primary" icon="upload"
          gate=${{ arm: "harness_install", guard }} />`}` : null}
      <${ResultBlock} lines=${p.lines} panel=${p} testid="harness-result" />
    </div>
  </div>`;
}

// Roll back: the daemon answers the first ask with the plan (409 REFUSED, error.data.plan);
// the confirm then binds to that plan's fingerprint, with the phrase for a re-key.
async function askRollback(bid, to) {
  const h = hv(bid);
  h.rollback = null;
  h.rollbackError = null;
  h.typed = "";
  h.boardTyped = "";
  h.autoRevert = null;
  closePick(bid);
  panelState(bid, "harness_install").lines = [];       // the last install's answer is not this one's
  const r = await timed(`harness rollback ${bid} --to ${to}`, () => call("harnessRollback", { bid }, { to }));
  const plan = r.error && r.error.data && r.error.data.plan;
  if (plan && r.error.errName === "REFUSED" && /confirm this plan/.test(r.error.message)) {
    h.rollback = { plan, to };
  } else if (r.error) {
    h.rollbackError = r.error;
  }
  changed();
}

function RollbackPanel({ bid, h }) {
  const { plan, to } = h.rollback;
  const p = panelState(bid, "harness_install");
  const spec = {
    key: "harness_rollback", label: `Roll back to ${plan.version}`, busyLabel: "Rolling back...", budgetS: 900,
    command: `harness rollback ${bid} --to ${to}`,
    run: (ctx) => runJob("harnessRollback", { bid }, { to, fingerprint: plan.fingerprint,
      ...(plan.rekey ? { rekey_phrase: h.typed.trim() } : {}), ...doorBody(h, plan) },
    (x) => ctx.progress(progressText(x), String(x.phase || "").split(":")[0]), "harness_rollback"),
    render: outcomeLines,
    renderError: (e) => (e.data && e.data.outcome ? outcomeLines(e.data.outcome) : []),
    onDone: (ok) => { h.typed = ""; if (ok) { h.rollback = null; refreshQuietly(bid); if (h.historyOpen) loadHistory(bid); } changed(); },
  };
  const guard = () => {
    if ((plan.blockers || []).length) return `blocked: ${plan.blockers[0]}`;
    if (plan.rekey && h.typed.trim() !== plan.consent_phrase) return `a re-key: type exactly ${plan.consent_phrase}`;
    return doorGuard(h, plan);
  };
  return html`<div class="card inset" data-testid="harness-rollback" data-release=${plan.version}>
    <div class="card-head"><h3 class="card-title"><${Icon} name="undo-2" />Roll back to harness ${plan.version}</h3>
      <span class="spacer"></span>
      <button type="button" class="btn ghost sm icon-only" aria-label="Close" data-action="rollback-close"
        onClick=${() => { h.rollback = null; changed(); }}><${Icon} name="x" /></button></div>
    <div class="card-body actions">
      <p class="secondary">A re-install of ${plan.version} through the same plan, fingerprint and consent as any install.</p>
      ${(plan.blockers || []).map((t) => html`<${Reason} key=${t} level="err" text=${t} />`)}
      ${(plan.warnings || []).map((t) => html`<${Reason} key=${t} level="warn" text=${t} />`)}
      <${PlanSteps} plan=${plan} />
      ${plan.rekey ? html`<div class="field rekey"><label for=${`hv-rb-${bid}`}>Consent</label>
        <input class="input mono grow" id=${`hv-rb-${bid}`} placeholder=${`type ${plan.consent_phrase}`} autocomplete="off" spellcheck="false"
          value=${h.typed} onInput=${(e) => { h.typed = e.target.value; changed(); }} data-testid="rollback-rekey" /></div>` : null}
      <${DoorConsent} bid=${bid} h=${h} plan=${plan} id="hv-rbp" />
      <${ArmBox} bid=${bid} armKey="harness_rollback" testid="arm-harness-rollback"
        text="Arm: I understand this writes the board's config SD (after a backup) and reboots the board." />
      <${ActionRow} bid=${bid} panel="harness_install" spec=${spec} variant="primary" icon="undo-2"
        gate=${{ arm: "harness_rollback", guard }} />
      <${ResultBlock} lines=${p.lines} panel=${p} testid="harness-result" />
    </div>
  </div>`;
}

function History({ bid, h }) {
  const d = h.history;
  if (h.historyError) return html`<${Reason} level="err" text=${`${h.historyError.errName}: ${h.historyError.message}`} />`;
  if (!d) return html`<p class="muted small"><${Spinner} /> Reading...</p>`;
  const rows = d.history || [];
  return html`<div data-testid="harness-history">
    ${rows.length ? html`<table class="table compact"><thead><tr><th>When</th><th>Harness</th><th>Result</th><th>Replaced</th></tr></thead>
      <tbody>${rows.map((x, i) => html`<tr key=${i} data-version=${x.version}>
        <td class="nowrap">${x.recorded_at ? `${new Date(x.recorded_at * 1000).toISOString().slice(0, 10)} ${clock(x.recorded_at)}` : "?"}</td>
        <td class="mono">${x.version || "?"}${x.kind && x.kind !== "install" ? html` <span class="tag">${x.kind}</span>` : null}${
          x.via === "hub" ? html` <span class="tag">via hub</span>` : null}${x.dark ? html` <span class="tag i-err">dark</span>` : null}</td>
        <td>${x.result}</td><td class="mono">${x.from_version || "-"}</td></tr>`)}</tbody></table>`
    : html`<p class="muted small" data-testid="harness-history-none">No install on ${bid} is recorded by this Harness Manager.</p>`}
  </div>`;
}

// --- the card ------------------------------------------------------------------------------------------

export function HarnessVersionsCard({ bid }) {
  const h = hv(bid);
  useEffect(() => { if (!h.catalog && !h.loading) loadCatalog(bid); }, [bid]);
  const p = panelState(bid, "harness");
  const pp = panelState(bid, "harness_pin");
  const cat = h.catalog;
  const board = (cat && cat.board) || {};
  const running = board.running || {};
  const rows = (cat && cat.releases) || [];
  const back = ((cat && cat.rollback) || []).find((c) => c.source === "history") || ((cat && cat.rollback) || [])[0];
  const refresh = {
    key: "harness_refresh", label: cat ? "Refresh" : "Refresh the list", busyLabel: "Refreshing...", budgetS: 120,
    command: `harness list ${bid}${h.all ? " --all" : ""}`,
    run: (ctx) => runJob("harnessRefresh", { bid }, refreshBody(bid), (d) => ctx.progress(d.phase || "channels"), "harness_refresh"),
    render: (out) => [{ kind: "ok", text: `${(out.releases || []).length} release(s) on ${(out.channels || []).map((c) => `${c.channel} #${c.serial}`).join(", ") || "no channel"}` }],
    onDone: (ok, out) => { if (ok && out && out.releases) setCatalog(bid, out); changed(); },
  };
  const rollbackWhy = !back ? "nothing to roll back to: no install is recorded and the channel has no older release"
    : !back.installable ? `Cannot: ${back.reason}` : "";
  const all = html`<label class="check-inline" title="Also list the beta and dev channels">
    <input type="checkbox" data-testid="harness-all" checked=${h.all} onChange=${(e) => { h.all = e.target.checked; changed(); }} />
    beta and dev</label>`;
  return html`<${Card} title="Harness versions" icon="layers" testid="harness-card"
      sub="Every signed release of this board's harness, with a verdict for this board. Installing one reprograms the board: the plan below says exactly what changes."
      actions=${html`${all}<${ActionRow} bid=${bid} panel="harness" spec=${refresh} icon="refresh-cw" compact=${true} gate=${{}} showReason=${false} />`}>
    <div class="stack gap-12">
      ${h.unavailable ? html`<${Reason} icon="circle-slash" testid="harness-unavailable" text=${`Harness versions are unavailable here: ${h.unavailable}.`} />` : null}
      ${h.error ? html`<${Reason} level="err" testid="harness-error" text=${`${h.error.errName}: ${h.error.message}`} />` : null}
      ${p.lines && p.lines.length ? html`<${ResultBlock} lines=${p.lines} panel=${p} testid="harness-refresh-result" />` : null}
      ${h.empty && !cat ? html`<${Reason} testid="harness-empty" text="No list yet: Refresh fetches the signed channel and plans every release for this board (nothing is installed)." />` : null}
      ${cat ? html`<dl class="kv" data-testid="harness-running">
          <dt>Running</dt><dd><b class="mono">${board.running_release || "unrecorded"}</b>
            ${running.shell_id ? html` · static <span class="mono">${running.shell_id}</span>` : null}
            ${running.firmware_sha ? html` · fw <span class="mono">${String(running.firmware_sha).slice(0, 8)}</span>` : null}
            ${running.impl ? ` · ${running.impl}` : ""}
            ${board.pinned ? html` <${Chip} level="held" icon="lock" testid="pinned-chip">pinned ${board.pinned}<//>` : null}</dd>
          <dt>Channels</dt><dd class="small">${(cat.channels || []).map((c) => html`<span key=${c.channel} class="chan"><span class="mono">${c.channel}</span> #${c.serial}${c.current ? ` (current ${c.current})` : ""}</span>`)}
            ${cat.at ? html`<span class="sub"> · listed ${clock(cat.at)}</span>` : null}</dd>
        </dl>
        <${LeaseLine} lease=${board.lease} />
        ${h.stale ? html`<${Reason} level="warn" testid="harness-stale" text=${`Changed since this list was built (${h.stale}): Refresh.`} />` : null}
        ${(cat.warnings || []).map((w) => html`<${Reason} key=${w} level="warn" text=${w} />`)}
        <ul class="hrows" data-testid="harness-rows">${rows.map((row) => html`<${Row} key=${row.version} bid=${bid} row=${row} h=${h} pinBusy=${!!pp.running} />`)}</ul>
        ${pp.lines && pp.lines.length ? html`<${ResultBlock} lines=${pp.lines} panel=${pp} testid="harness-pin-result" />` : null}` : null}
      ${h.pick ? html`<${InstallPanel} bid=${bid} h=${h} />` : null}
      ${h.rollback ? html`<${RollbackPanel} bid=${bid} h=${h} />` : null}
      ${cat ? html`<div class="row hv-foot">
        <button type="button" class="btn sm" data-action="harness-history" aria-expanded=${h.historyOpen ? "true" : "false"}
          onClick=${() => { h.historyOpen = !h.historyOpen; if (h.historyOpen) loadHistory(bid); changed(); }}>
          <${Icon} name="history" /> History</button>
        <button type="button" class="btn sm" data-action="harness-rollback" aria-disabled=${rollbackWhy ? "true" : undefined}
          title=${rollbackWhy || `Re-install ${back.version}: ${back.why}`}
          onClick=${() => { if (!rollbackWhy) askRollback(bid, "previous"); }}>
          <${Icon} name="undo-2" /> Roll back${back && back.installable ? ` to ${back.version}` : ""}</button>
        ${rollbackWhy ? html`<span class="secondary small" data-testid="rollback-why">${rollbackWhy}</span>`
          : html`<span class="secondary small">${back.why}</span>`}
      </div>` : null}
      ${h.rollbackError ? html`<${Reason} level="err" testid="rollback-error" text=${`${h.rollbackError.errName}: ${h.rollbackError.message}${h.rollbackError.holder ? ` (holder: ${h.rollbackError.holder})` : ""}`} />` : null}
      ${h.historyOpen && cat ? html`<${History} bid=${bid} h=${h} />` : null}
      <p class="secondary small" data-testid="harness-not-yet"><${Icon} name="info" cls="sm" /> ${NOT_YET}</p>
    </div>
  <//>`;
}
