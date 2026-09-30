// Board › Versions (UI v2 round 3, lane UI2-BOARD): two cards side by side. Left, what the
// board boots from: the user microSD's OS slots A/B (GET /slots) with the overlay store and
// Roll back (G6: POST /slots/rollback, /card/commit, /card/clear), the hub's netboot image,
// or the configuration SD with Back up now and the interrupted-install recovery. Right, the
// signed releases with a verdict each (harness.js; on Linux a release IS its OS image), and
// an Install… plan inline in its row.
//
// The older channel checker and "The app" card are gone (UI_V2_PLAN.md BD16): the harness
// catalogue does the check, and the app's own update lives in its banner and Settings.

import { gateReason, interlock, panelState, runAction, runJob } from "../actions.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed, jobLabel, loadCard, openActivity, scheduleRefresh } from "../store.js";
import { ArmBox, Card, Chip, Icon, Reason, ResultBlock, Spinner } from "../ui.js";
import {
  identityOf, isLinux, loadSlots, mccRoute, osKind, slotsOf, USB_WORDS,
} from "./boardfacts.js";
import {
  HarnessRollbackFoot, HarnessRollbackPanels, offeredText, ReleasesCard,
} from "./harness.js";
import { heldLines } from "./power.js";
import { ConfigSdCard, SdRecovery } from "./sd.js";

const other = (x) => (x === "A" ? "B" : "A");
const pct = (got, len) => (len ? Math.min(100, Math.floor((got * 100) / len)) : 0);
const mb = (n) => (Number.isFinite(Number(n)) && n ? `${(Number(n) / 1e6).toFixed(1)} MB` : "");

// --- the OS slots ----------------------------------------------------------------------------------

function SlotTile({ id, os }) {
  const s = (os.slots || {})[id] || {};
  const job = os.job || {};
  const run = os.running === id;
  const dft = os.default === id;
  let chip;
  let line;
  let cls = "";
  let meter = null;
  if (job.slot === id && (job.state === "writing" || job.state === "verifying")) {
    const len = job.len || job.length || 0;
    const p = pct(job.got || 0, len);
    chip = ["accent", "loader-circle", `${job.state === "writing" ? "Writing" : "Reading back"} ${p}%`];
    line = job.text || (job.state === "writing" ? `${mb(job.got)} of ${mb(len)}; the old image is gone until this ends` : "the board checks every region's CRC");
    cls = "busy";
    meter = p;
  } else if (!s.state || s.state === "empty" || !s.version) {
    chip = ["warn", "triangle-alert", s.err ? "Empty: torn write" : "Empty"];
    line = s.err || "no image: nothing boots from it";
    cls = "torn";
  } else if (os.fell_back === id) {
    chip = ["err", "circle-x", "Did not boot"];
    line = `stage0 went back to ${other(id)}; ${id} is still the default`;
    cls = "bad";
  } else if (run && dft) {
    const ok = os.confirmed !== false && s.verified === "boot";
    chip = ok ? ["ok", "circle-check", "Running · default"] : ["accent", "loader-circle", "Booted · confirming"];
    line = s.boot || (ok ? "booted and confirmed" : "harnessd confirms a healthy boot");
    cls = "run";
  } else if (run) {
    chip = ["warn", "triangle-alert", "Running as the fallback"];
    line = s.boot || "the default did not come up, so stage0 booted this";
    cls = "run";
  } else if (dft) {
    chip = ["accent", "chevron-right", "Default: boots next"];
    line = s.boot || "written; it boots at the next start";
    cls = "busy";
  } else {
    chip = ["", "history", "Fallback"];
    line = s.boot || "stage0 boots it if the default fails twice";
  }
  return html`<div class=${`os-slot ${cls}`} data-testid=${`slot-${id}`} data-state=${s.state || "empty"}
      data-running=${run ? "yes" : "no"} data-default=${dft ? "yes" : "no"}>
    <div class="os-slot-h"><span class="os-letter">Slot ${id}</span><span class="os-rel">${s.version || "empty"}</span>
      ${s.sid ? html`<span class="mono small muted">${s.sid}</span>` : null}</div>
    <${Chip} level=${chip[0]} icon=${chip[1]}>${chip[2]}<//>
    ${meter !== null ? html`<div class="meter"><div style=${{ width: `${meter}%` }}></div></div>` : null}
    <div class="small muted">${line}</div></div>`;
}

function afterCardJob(bid) {
  loadSlots(bid, { background: false });
  loadCard(bid);
  scheduleRefresh(bid, 300);
}

function slotRollbackSpec(bid, label) {
  const b = boardState(bid);
  return {
    key: "slot_rollback", label, busyLabel: "Rolling back...", budgetS: 2400,
    command: "slot rollback --confirm",
    run: (ctx) => runJob("slotRollback", { bid }, { confirm: true }, (d) => {
      b.slotJob = { phase: d.phase || "", text: d.text || "", eta: d.eta_s || null };
      changed();
      ctx.progress(d.text || `${d.phase || "rollback"}${d.total > 1 ? `: ${Math.floor((d.done * 100) / d.total)}%` : ""}`, d.phase);
    }, "slot_rollback"),
    render: (r) => [{ kind: "ok", text: r && r.note ? r.note : `slot ${(r && r.slot) || "?"} is the default${r && r.rebooted ? " and runs" : ""}` }],
    renderError: heldLines,
    onDone: () => { b.slotJob = null; afterCardJob(bid); },
  };
}

// The steps the service runs for a roll back (G6, services/slots.py): read the other slot back
// when this boot did not, make it the default, reboot into it and confirm.
function rollbackSteps(os, x) {
  const s = (os.slots || {})[x] || {};
  const steps = [];
  if (s.verified !== "boot" && s.verified !== "readback") steps.push([`Read back slot ${x}`, "it was not read back this boot, so the board checks it first", true]);
  steps.push([`Make ${x} the default`, "the boot-select sector is rewritten and read back", false]);
  if (os.running !== x) {
    steps.push([`Reboot into ${x}`, "warm reboot, witnessed", false]);
    steps.push(["Confirm", "the board must be running it", false]);
  }
  return steps;
}

function StepBars({ steps }) {
  return html`<ol class="os-steps">${steps.map(([t, d, card], i) => html`<li key=${t} class=${`os-step ${card ? "sd" : ""}`} title=${d}>
    <div class="bar"><i></i></div><span class="n">${i + 1} · ${t}</span></li>`)}</ol>`;
}

// A button of this page (armed, gated with the lease holder, its result under it).
function Drive({ bid, panel, spec, gate, armText, armTestid, variant = "", icon = "" }) {
  const p = panelState(bid, panel);
  const why = gateReason(bid, panel, spec.key, gate);
  const running = p.running === spec.key;
  const blocked = !!why && !running;
  const quiet = why.startsWith("not armed");
  return html`<div class="stack gap-8">
    <div class="row"><${ArmBox} bid=${bid} armKey=${gate.arm} compact=${true} testid=${armTestid} text=${armText} />
      <button type="button" class=${`btn sm ${blocked ? "" : variant}`} data-action=${spec.key}
        aria-disabled=${blocked ? "true" : undefined} aria-busy=${running ? "true" : undefined} title=${blocked ? why : spec.label}
        onClick=${() => { if (running) return; if (why) { interlock(bid, panel, spec.command, why); return; } runAction(bid, panel, { ...spec, arm: gate.arm }); }}>
        ${running ? html`<${Spinner} /> ${p.busyLabel}` : html`${icon ? html`<${Icon} name=${icon} />` : null}${spec.label}`}</button></div>
    ${why && !running ? html`<p class=${`reason ${quiet ? "sr-only" : "warn"}`} data-testid=${`reason-${spec.key}`}><${Icon} name="info" /><span>${why}</span></p>` : null}
    ${p.lines && p.lines.length ? html`<${ResultBlock} lines=${p.lines} panel=${p} testid=${`${spec.key}-result`} />` : null}
  </div>`;
}

function OverlayStore({ bid }) {
  const b = boardState(bid);
  const c = b.card;
  if (!c || !c.store) return null;
  if (!c.present) return html`<div class="os-store"><${Icon} name="layers" cls="sm" /><span>Overlay store: no card in the slot.</span></div>`;
  const d = c.default;
  const clear = {
    key: "card_clear", label: "Clear", busyLabel: "Clearing...", budgetS: 120, command: "card clear --confirm",
    run: () => runJob("cardClear", { bid }, { confirm: true }, null, "card_clear"),
    render: (r) => [{ kind: "ok", text: (r && (r.note || r.line)) || "the power-on default is cleared" }],
    renderError: heldLines, onDone: () => afterCardJob(bid),
  };
  const commit = {
    key: "card_commit", label: "Keep the running design", busyLabel: "Keeping...", budgetS: 300, command: "card commit --confirm",
    run: () => runJob("cardCommit", { bid }, { confirm: true }, null, "card_commit"),
    render: (r) => [{ kind: "ok", text: (r && (r.note || r.line)) || "kept on the card" }],
    renderError: heldLines, onDone: () => afterCardJob(bid),
  };
  return html`<div class="stack gap-8" data-testid="overlay-store">
    <div class="os-store"><${Icon} name="layers" cls="sm" /><span>Overlay store: boots <b>${d && d.rm_name ? d.rm_name : "greybox"}</b> at power-on${d && d.rm_name ? ` (kept, slot ${d.slot || "?"})` : " (nothing kept)"}.</span></div>
    ${d && d.rm_name ? html`<${Drive} bid=${bid} panel="card" spec=${clear} armTestid="arm-card-clear"
        gate=${{ arm: "card_clear", holder: "Clear the card default" }} armText="Arm: the greybox loads at the next power-on." icon="x" />`
      : c.committable ? html`<${Drive} bid=${bid} panel="card" spec=${commit} armTestid="arm-card-commit"
        gate=${{ arm: "card_commit", holder: "Keep on the card" }} armText="Arm: the running design loads at every power-on." icon="layers" />` : null}
  </div>`;
}

function CardHere({ bid }) {
  const b = boardState(bid);
  const os = slotsOf(b) || {};
  const v = (os.slots || {});
  const run = os.running;
  const x = run ? other(run) : null;
  const inX = x ? v[x] || {} : {};
  const fell = os.fell_back;
  const p = panelState(bid, "slot_rollback");
  const busy = p.running === "slot_rollback";
  const canBack = !fell && x && inX.state === "valid" && inX.version;
  const planOpen = !!b.slotPlan;
  const card = b.card || {};
  const running = (v[run] || {}).version || identityOf(b).harness_version || "?";
  return html`<${Card} title="User microSD" icon="memory-stick" cls="os-here" testid="os-here"
      sub="Two OS slots: one runs, the other is the fallback.">
    <div class="os-card-line">${card.card_mb ? `${(card.card_mb / 1024).toFixed(0)} GB card · ` : ""}slots A and B · the overlay store${b.slotsLoading ? html` · <${Spinner} /> reading` : ""}</div>
    ${b.slotsError ? html`<${Reason} level="err" text=${`${b.slotsError.errName}: ${b.slotsError.message}`} />` : null}
    <div class="os-slots"><${SlotTile} id="A" os=${os} /><${SlotTile} id="B" os=${os} /></div>
    ${fell ? html`<div class="outcome warn os-fell" data-testid="slot-fell-back"><${Icon} name="triangle-alert" />
        <div class="stack gap-8"><span><b>Slot ${fell} did not boot.</b> Stage0 went back to ${run} (${(v[run] || {}).version || "?"}), but ${fell} is still the default. Making ${run} the default again takes seconds: no reboot.</span>
          <${Drive} bid=${bid} panel="slot_rollback" spec=${slotRollbackSpec(bid, `Make ${run} the default`)} armTestid="arm-slot-fix"
            gate=${{ arm: "slot_rollback", holder: "Roll back the OS slot" }} armText=${`Arm: slot ${run} becomes the default again.`} icon="undo-2" /></div></div>` : null}
    ${(os.notes || []).map((n) => html`<p class="small muted mt-8" key=${n}>${n}</p>`)}
    <${OverlayStore} bid=${bid} />
    <div class="bt-foot">
      ${canBack ? html`<button type="button" class="btn sm" data-action="slot-rollback-plan" aria-expanded=${planOpen ? "true" : "false"}
          onClick=${() => { b.slotPlan = !planOpen; changed(); }}><${Icon} name="undo-2" /> Roll back to ${inX.version}</button>`
        : html`<span class="small muted">${fell ? `Roll back = make ${run} the default (above).` : "Nothing to roll back to on the card."}</span>`}
      <span class="grow"></span><button type="button" class="link-btn" onClick=${() => openActivity(bid)}>History in Activity</button>
      ${canBack ? html`<span class="small muted os-rb-note">slot ${x} holds ${inX.version}: the board boots it (running ${running} now).</span>` : null}
    </div>
    ${canBack && (planOpen || busy) ? html`<div class="os-plan" data-testid="slot-rollback">
      <div class="os-plan-h"><b>${busy ? "Rolling back to" : "Roll back to"} ${inX.version}</b><span>slot ${x} becomes the default again</span>
        <span class="grow"></span>${busy ? null : html`<button type="button" class="btn ghost sm icon-only" aria-label="Close" title="Cancel: close this plan"
          onClick=${() => { b.slotPlan = false; changed(); }}><${Icon} name="x" /></button>`}</div>
      <${StepBars} steps=${rollbackSteps(os, x)} />
      ${busy && b.slotJob ? html`<div class="progress-box"><div class="meter-line"><span>${b.slotJob.text || b.slotJob.phase || "working"}</span>
        ${b.slotJob.eta ? html`<span class="num">about ${Math.ceil(b.slotJob.eta / 60)} min left</span>` : null}</div></div>` : null}
      ${(inX.verified !== "boot" && inX.verified !== "readback") ? html`<div class="outcome warn os-safe"><${Icon} name="triangle-alert" />
        <span><b>Don't reboot, restart the shell or cut the power during step 1.</b> The service refuses a reboot while the card is read back.</span></div>` : null}
      <${Drive} bid=${bid} panel="slot_rollback" spec=${slotRollbackSpec(bid, `Roll back to ${inX.version}`)} armTestid="arm-slot-rollback"
        gate=${{ arm: "slot_rollback", holder: "Roll back the OS slot" }} armText="Arm: this makes the other slot the default and reboots the board into it." icon="undo-2" variant="primary" />
    </div>` : null}
  <//>`;
}

function NetbootHere({ bid }) {
  const b = boardState(bid);
  const id = identityOf(b);
  const why = (b.slots && b.slots.reason) || (b.card && b.card.reason) || "";
  const hub = b.week && b.week.hub;
  return html`<${Card} title="Netboot" icon="server" cls="os-here" testid="os-here"
      sub="No user microSD: the hub serves the image at every cold boot.">
    <div class="os-net" data-testid="os-netboot">
      <div class="os-net-h"><${Icon} name="server" /><b>${hub ? `From ${hub.host}` : "From the hub"}</b><${Chip} level="plain">no OS slots<//></div>
      <div class="small secondary">Runs harness ${id.harness_version || "?"} in RAM. At every cold boot stage0 waits in rescue and the hub pushes the image over TFTP. <span class="mono">/persist</span> is tmpfs: nothing on the board survives a reboot.</div>
      ${why ? html`<div class="small muted">${why}</div>` : null}
    </div>
    <div class="bt-foot"><span class="small muted">Roll back: ask for the previous image (a release's Ask for it…).</span>
      <span class="grow"></span><button type="button" class="link-btn" onClick=${() => openActivity(bid)}>History in Activity</button></div>
  <//>`;
}

function routeText(bid) {
  const r = mccRoute(bid);
  return `${(USB_WORDS[r.to] || USB_WORDS.unknown).nav}${r.reason ? `: ${r.reason}` : ""}`;
}

export function VersionsPage({ bid }) {
  const b = boardState(bid);
  const lx = isLinux(b);
  useEffect(() => { if (lx && !b.slotsLoading) loadSlots(bid); }, [bid, lx]);
  const kind = osKind(bid);
  let here;
  if (kind === "card") here = html`<${CardHere} bid=${bid} />`;
  else if (kind === "netboot") here = html`<${NetbootHere} bid=${bid} />`;
  else if (kind === "bm") {
    here = html`<${ConfigSdCard} bid=${bid} route=${routeText(bid)}
      foot=${html`<${HarnessRollbackFoot} bid=${bid} /><span class="grow"></span>
        <button type="button" class="link-btn" onClick=${() => openActivity(bid)}>History in Activity</button>`}
      after=${html`<${HarnessRollbackPanels} bid=${bid} />`} />`;
  } else {
    here = html`<${Card} title="What this board boots from" icon="hard-drive" cls="os-here" testid="os-here">
      <${Reason} icon="loader-circle" text=${b.infoError ? `The board's last read failed: ${b.infoError.message}` : `Reading ${lx ? "the OS slots" : "the board"}...`} /><//>`;
  }
  return html`<div class="os-page">
    <div class="stack">
      <${SdRecovery} bid=${bid} />
      ${here}
    </div>
    <${ReleasesCard} bid=${bid} linux=${lx} netboot=${kind === "netboot"} />
  </div>`;
}

// The Versions page's status line in the side list: [text, dot, title].
export function versionsStatus(bid) {
  const b = boardState(bid);
  if (b.pending) return ["SD install interrupted", "err", "An SD install stopped part-way: restore it first"];
  const install = panelState(bid, "harness_install");
  const h = b.harness;
  if (install.running) {
    return [`${install.running === "harness_rollback" ? "rolling back" : "installing"} ${(h && h.pick) || ""}${h && h.progress ? ` · ${h.progress}` : ""}`.trim(), "accent", "An install is running on the board"];
  }
  const kinds = ["harness_install", "harness_rollback", "slot_rollback", "card_commit", "card_clear", "sd_install", "sd_restore", "update_harness"];
  if (b.job && kinds.includes(b.job.kind)) return [`${jobLabel(b.job.kind)}...`, "accent", `A ${jobLabel(b.job.kind)} job runs on the board`];
  const os = slotsOf(b);
  if (os && os.job && (os.job.state === "writing" || os.job.state === "verifying")) {
    return [os.job.text || `${os.job.state} slot ${os.job.slot}`, "accent", "The user microSD is busy"];
  }
  if (os && os.fell_back) return [`slot ${os.fell_back} did not boot`, "warn", `Slot ${os.fell_back} failed to boot; ${os.running} runs as the fallback`];
  const empty = os && ["A", "B"].find((x) => !((os.slots || {})[x] || {}).version);
  if (empty) return [`slot ${empty} is empty`, "warn", `Slot ${empty} holds no image`];
  if (!b.info) return ["not read yet", null, "The board has not been read yet"];
  const ver = identityOf(b).harness_version || "?";
  const offered = offeredText(bid);
  const kind = osKind(bid);
  const from = kind === "netboot" ? ", netbooted" : kind === "card" ? `, from slot ${os.running} of the user microSD` : kind === "bm" ? ", bare-metal" : "";
  return [`${ver}${offered ? ` · ${offered} offered` : ""}`, null, `Harness ${ver}${from}${offered ? `; ${offered} is offered` : ""}`];
}

