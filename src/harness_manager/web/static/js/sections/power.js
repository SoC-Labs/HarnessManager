// Board › Recover (UI v2 round 3, lane UI2-BOARD): one ladder, gentlest first. Each step is one
// dense row: its title, severity and typical time, a one-line summary (the full text on hover),
// what it keeps (DUT state, consoles, design, lease, FPGA config) as a small matrix, then Arm
// and the button. Steps this board cannot do say why; restart, reboot and power-cycle wait
// while the card is being written (the service refuses them too: SLOT-TIMING's reset guard).
//
// Every button goes through actions.js gateReason with `holder` (the one lease rule): on a
// board someone else leases it is refused with that reason, in the page and by the service
// (G7: 409 HELD, shown plainly in the row). The action specs are exported: the Overview's
// Board tile runs the same actions, with the same panels, so a result shows in both places.

import { gateReason, interlock, panelState, runAction, runJob } from "../actions.js";
import { call, toApiError } from "../api.js";
import { deployBar, elapsedSince } from "../format.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed, jobLabel, navigate, scheduleRefresh } from "../store.js";
import { loadPower, week } from "../week.js";
import { ArmBox, Card, Icon, MiniBar, Reason, ResultBlock, Spinner } from "../ui.js";
import {
  cardGuard, driveWhy, identityOf, isLinux, loadSlots, mccRoute, osKind, rangeText,
} from "./boardfacts.js";

// The engine picks the reboot wait by harness implementation (bare-metal ~120 s, Linux
// 300 s: constants.reboot_wait_s), so the UI sends none and budgets for the longer one.
const REBOOT_BUDGET_S = 330;

export const ARM_TEXT = {
  reset_dut: "Arm: I understand this resets the DUT CPU (the harness keeps running).",
  reboot: "Arm: I understand this restarts the whole board; every session, console and loaded overlay is lost.",
  power_cycle: "Arm: I understand this cuts the board's power; everything on it stops and it boots from its SD.",
  restore: "Arm: I understand this swaps the partition to the greybox; the DUT stops.",
  reset_shell: "Arm: I understand every console and the debug session drop while the shell restarts.",
};

export function resetTargets(bid) {
  const s = boardState(bid).session;
  return s && Array.isArray(s.reset_targets) ? s.reset_targets : null;   // null: not read
}

// G7: the service's own refusal, in words (the lease is someone else's, a job holds the
// board, the card is busy). errorLines already shows the name, message and holder.
export function heldLines(e) {
  const err = toApiError(e);
  if (err.errName !== "HELD") return [];
  const d = err.data || {};
  if (d.reason === "LEASE") {
    const who = (d.lease && d.lease.holder) || err.holder || "someone else";
    return [{ kind: "warnline", text: `The service refused it: the hub lease is ${who}'s, and only the lease holder drives this board. Nothing was sent to the board.` }];
  }
  if (d.job) return [{ kind: "warnline", text: `The service refused it: a ${jobLabel(d.kind)} job holds the board. Nothing was sent.` }];
  return [{ kind: "warnline", text: "The service refused it (HELD). Nothing was sent to the board." }];
}

export function resetDutSpec(bid, target = "dut") {
  return {
    key: "reset_dut", label: "Reset DUT", busyLabel: "Resetting...", budgetS: 20,
    command: `reset ${target}`,
    run: async () => { await call("reset", { bid }, { target }); return `reset ${target}: done`; },
    render: (t) => [{ kind: "ok", text: t }],
    renderError: heldLines,
  };
}

// FIX-PACK-4: `holder`: on a hub board, the lease holder only (actions.js gateReason).
export const RESET_DUT_GATE = { capability: "reset_dut", adapter: "resets", arm: "reset_dut", holder: "Reset DUT" };

export function rebootSpec(bid) {
  return {
    key: "reboot", label: "Reboot board", busyLabel: "Rebooting...", budgetS: REBOOT_BUDGET_S,
    command: "mcc reboot",
    run: (ctx) => runJob("reboot", { bid }, {},
      (d) => ctx.progress(`reboot: ${d.phase} (${d.done}/${d.total})`, d.phase), "reboot"),
    render: rebootLines,
    renderError: heldLines,
    onDone: () => scheduleRefresh(bid, 200),
  };
}

export const REBOOT_GATE = { capability: "reboot_board", adapter: "controller", arm: "reboot", holder: "Reboot" };

// The reboot job's result is the controller's evidence: {summary, down_after_s, up_after_s,
// down_evidence[], up_evidence, shell_id_before, shell_id_after, fpga_configured, fpga_file,
// mcc_firmware, ...} (MCC-FIX: fpga_file is the .bit the MCC said it loaded, "" when unknown).
export function rebootLines(ev) {
  if (!ev || typeof ev !== "object") return [{ kind: "ok", text: "the board went down and came back" }];
  const out = [{ kind: "ok", text: ev.summary || "the board went down and came back" }];
  const t = (s) => (Number.isFinite(Number(s)) ? `${Number(s).toFixed(1)} s` : "?");
  if (ev.down_after_s !== undefined || ev.up_after_s !== undefined) {
    out.push({ kind: "out", text: `down after ${t(ev.down_after_s)}, back after ${t(ev.up_after_s)}` });
  }
  if (ev.shell_id_before || ev.shell_id_after) {
    const same = ev.shell_id_before === ev.shell_id_after;
    out.push({ kind: same ? "out" : "warnline",
      text: `shell ${ev.shell_id_before || "?"} -> ${ev.shell_id_after || "?"}${same ? " (unchanged)" : " (CHANGED)"}` });
  }
  if (ev.fpga_configured !== undefined) {
    out.push({ kind: ev.fpga_configured ? "out" : "warnline",
      text: `FPGA configured: ${ev.fpga_configured ? "yes" : "NO"}` });
  }
  if (ev.fpga_file) out.push({ kind: "out", text: `MCC loaded ${ev.fpga_file}` });
  for (const line of [].concat(ev.down_evidence || [])) out.push({ kind: "hint", text: `down: ${line}` });
  if (ev.up_evidence) out.push({ kind: "hint", text: `up: ${typeof ev.up_evidence === "object" ? JSON.stringify(ev.up_evidence) : ev.up_evidence}` });
  return out;
}

// --- the supply's cycle -----------------------------------------------------------------------

// POST .../power/cycle takes off_s in 2..300 s (400 USAGE otherwise); the daemon's default is 5.
export const OFF_S = { min: 2, max: 300, fallback: 5 };

export function offSeconds(b) {
  const raw = b.powerOffS;
  if (raw === undefined || raw === null || raw === "") return OFF_S.fallback;
  return Number(raw);
}

function offGuard(b) {
  const s = offSeconds(b);
  return Number.isFinite(s) && s >= OFF_S.min && s <= OFF_S.max
    ? "" : `the off time must be ${OFF_S.min} to ${OFF_S.max} seconds`;
}

// The job's result: {meter, off_s, was_on, confirmed_off, confirmed_on, seconds}. The outlet
// only proves the supply is back; nothing here claims the board is up.
export function cycleLines(r) {
  if (!r || typeof r !== "object") return [{ kind: "ok", text: "the outlet cycled" }];
  const out = [{ kind: r.confirmed_off && r.confirmed_on ? "ok" : "warnline",
    text: `${r.meter || "the outlet"}: off ${r.off_s} s, ${r.confirmed_off ? "OFF confirmed" : "OFF NOT confirmed"}, ${r.confirmed_on ? "ON confirmed" : "ON NOT confirmed"} (${r.seconds} s)` }];
  if (r.was_on === false) out.push({ kind: "warnline", text: "the outlet was already off before the cycle" });
  out.push({ kind: "hint", text: "The supply is back; the board boots by itself. The header re-reads the board; give the shell about 15 s." });
  return out;
}

function powerCycleSpec(bid, device) {
  const b = boardState(bid);
  const off = offSeconds(b);
  return {
    key: "power_cycle", label: "Power-cycle", busyLabel: "Cycling...", budgetS: off + 60,
    command: `power cycle --off ${off}${device ? ` (${device})` : ""}`,
    run: (ctx) => runJob("powerCycle", { bid }, { off_s: off }, (d) => ctx.progress(`outlet ${d.phase}`, d.phase), "power_cycle"),
    render: cycleLines,
    renderError: heldLines,
    onDone: () => { loadPower(bid); scheduleRefresh(bid, 200); },
  };
}

function restoreSpec(bid) {
  return {
    key: "restore", label: "To greybox", busyLabel: "Swapping...", budgetS: 120, command: "restore",
    run: (ctx) => runJob("restore", { bid }, undefined,
      (d) => ctx.progress(`${d.phase || "restore"}${d.total > 1 ? `: ${Math.floor((d.done * 100) / d.total)}%` : ""}`, d.phase), "restore"),
    render: (r) => [{ kind: "ok", text: r && r.rm_name ? `the partition runs ${r.rm_name}${r.verified ? " (verified)" : ""}` : "greybox restored" }],
    renderError: heldLines,
    onDone: () => scheduleRefresh(bid, 200),
  };
}

function shellSpec(bid) {
  return {
    key: "reset_shell", label: "Restart", busyLabel: "Restarting...", budgetS: 60, command: "reset shell",
    run: async () => { await call("reset", { bid }, { target: "shell" }); return "reset shell: done"; },
    render: (t) => [{ kind: "ok", text: t }],
    renderError: heldLines,
    onDone: () => scheduleRefresh(bid, 500),
  };
}

// --- the ladder ------------------------------------------------------------------------------

export const KEEP_COLS = [["dut", "DUT state", "DUT"], ["con", "Consoles", "Consoles"], ["des", "Design", "Design"],
  ["lea", "Lease", "Lease"], ["fpga", "FPGA config", "FPGA"]];
const KEEP_ICON = { kept: "check", lost: "x", back: "refresh-cw", held: "circle-pause", reload: "refresh-cw" };
const KEEP_WORD = { kept: "kept", lost: "lost", back: "drops", held: "held", reload: "reloads" };
const KEEP_LONG = { kept: "kept", lost: "lost", back: "drops, then comes back", held: "held in reset", reload: "reloaded" };

const K = (dut, con, des, lea, fpga) => ({ dut, con, des, lea, fpga });

const VIA = { hub: "through the hub's Debug USB", pc: "through this PC's Debug USB", self: "through the board's own USB loop" };

// What the design comes back as after a cold start: the card's power-on default, else greybox.
function comesBackAs(b) {
  const d = b.card && b.card.default;
  return d && d.rm_name ? `${d.rm_name} (kept on the card)` : "greybox";
}

function isGreybox(b) {
  const id = identityOf(b);
  return id.rm_name === "greybox" || (id.rm_id !== undefined && Number(id.rm_id) === 0 && id.rm_id !== "");
}

// The five steps for this board: facts (title, time, keeps) and how each runs (spec, gate).
export function recoverSteps(bid) {
  const b = boardState(bid);
  const w = week(bid);
  const lx = isLinux(b);
  const route = mccRoute(bid).to;
  const via = VIA[route];
  const back = comesBackAs(b);
  const net = osKind(bid) === "netboot";
  const guard = () => {
    const g = cardGuard(bid);
    return g ? `waits: ${g}` : "";
  };
  const shellGuard = () => {
    const t = resetTargets(bid);
    if (t && !t.includes("shell")) return "Cannot: this board's reset adapter offers no 'shell' target";
    return guard();
  };
  const pw = w.power;
  const plug = !!(pw && !pw.cycle_reason);
  return [
    { k: "reset", key: "reset_dut", panel: "reset_dut", sev: 1, t: "Reset DUT", bt: "Reset DUT", icon: "rotate-ccw",
      testid: "reset-dut", result: "reset-dut-result", arm: "arm-reset-dut",
      sh: "Pulses the DUT's reset; all else stays.", time: "~1 s",
      d: "Pulses the DUT's reset through the harness. The harness, consoles and design stay; the boot banner prints on the Workbench console.",
      keep: K(["lost", "restarts from its reset vector"], ["kept"], ["kept"], ["kept"], ["kept"]),
      spec: resetDutSpec(bid, "dut"), gate: RESET_DUT_GATE },
    { k: "greybox", key: "restore", panel: "rc_greybox", sev: 2, t: "Back to greybox", bt: "To greybox", icon: "undo-2",
      testid: "rung-greybox", result: "greybox-result", arm: "arm-greybox",
      sh: "Swaps in the baseline; the shell runs on.", time: "~40 s",
      d: "Swaps the partition to the baseline design (greybox): the gentlest design-level step. The shell keeps running.",
      keep: K(["lost"], ["kept", "the DUT console goes quiet: greybox has no CPU"], ["lost", "greybox until you program again"], ["kept"], ["kept", "the shell stays"]),
      no: isGreybox(b) ? "already greybox" : "",
      spec: restoreSpec(bid), gate: { capability: "deploy_partial", arm: "rc_greybox", holder: "Restore baseline" } },
    { k: "restart", key: "reset_shell", panel: "reset_shell", sev: 3, t: "Restart the shell", bt: "Restart", icon: "refresh-cw",
      testid: "reset-shell", result: "reset-shell-result", arm: "arm-reset-shell",
      sh: lx ? "Restarts the board's Linux; the FPGA is not reloaded." : "Warm firmware restart; consoles reconnect.",
      time: lx ? "~3 min" : "~3 s",
      d: lx ? `Restarts the board's Linux: the board's watchdog resets it and it boots again (about 3 minutes); the FPGA is not reloaded. ${net ? "This board netboots, so it waits in stage0 rescue until its image is pushed again; it does not come back by itself. " : ""}Consoles and debug drop${net ? "" : ", then reconnect by themselves"}. The design stays loaded but held in reset until you program once.`
        : "Restarts the harness firmware (warm; the FPGA is not reloaded). Consoles and debug drop, then reconnect; the design stays loaded.",
      keep: K(["lost"],
        lx ? (net ? ["lost", "drop and stay down until the image is pushed again"] : ["back", "drop for ~3 min, then reconnect"]) : ["back", "drop for ~3 s, then reconnect"],
        lx ? ["held", "loaded, but held in reset until you program once"] : ["kept"], ["kept"], ["kept"]),
      spec: shellSpec(bid),
      gate: { capability: "reset_shell", adapter: "resets", arm: "reset_shell", holder: "Restart shell", guard: shellGuard } },
    { k: "reboot", key: "reboot", panel: "reboot", sev: 4, t: "Reboot via the MCC", bt: "Reboot", icon: "power",
      testid: "reboot-card", result: "reboot-result", arm: "arm-reboot",
      sh: lx ? "Cold: the FPGA reloads, then Linux boots." : "Cold: the FPGA reloads from the config SD.",
      time: lx ? "~190 s" : "~60 s",
      d: lx ? `Cold: the MCC reloads the FPGA from the config SD${via ? ` ${via}` : ""}, then Linux boots through stage0 (~190 s to the control port). ${net ? "This board netboots, so it waits in stage0 rescue until the hub pushes its image again." : `The design comes back as ${back}.`}`
        : `Cold: the MCC reloads the FPGA from the config SD${via ? ` ${via}` : ""} (~60 s). The design comes back as ${back}.`,
      keep: K(["lost"], ["back", `drop for ${lx ? "~3 min" : "~1 min"}, then reconnect`], ["lost", `comes back as ${back}`],
        ["kept", "the hub lease is the hub's: a reboot does not touch it"], ["reload", "reloaded from the config SD"]),
      note: route === "self" ? "Through the board's own loop: it works while the harness runs. A wedged board cannot reboot itself; then power-cycle it, or press PB0 at the board." : "",
      fix: route === "none" ? "Connect J8 (Debug USB) to the hub or to this PC." : "",
      spec: rebootSpec(bid), gate: { ...REBOOT_GATE, guard } },
    { k: "power", key: "power_cycle", panel: "power_cycle", sev: 5, t: "Power-cycle", bt: "Power-cycle", icon: "plug-zap",
      testid: "power-card", result: "power-result", arm: "arm-power",
      sh: "Cuts the power: MCC and FPGA start cold.", time: lx ? "~4 min" : "~90 s",
      d: `Cuts the power${pw && pw.device ? ` through ${pw.device}` : " with a networked plug"}: the MCC and the FPGA start from nothing.`,
      keep: K(["lost"], ["back", "drop, then reconnect"], ["lost", back], ["kept"], ["reload", "reloaded from the config SD"]),
      hidden: !plug,
      noText: !pw ? "" : pw.cycle_reason || "",
      spec: powerCycleSpec(bid, pw && pw.device),
      gate: { arm: "power_cycle", holder: "Power-cycle", guard: () => offGuard(b) || guard() } },
  ];
}

function KeepCell({ col, v }) {
  const [st, note] = v;
  return html`<span class=${`kp ${st}`} title=${`${col}: ${note || KEEP_LONG[st]}`} data-keep=${st}>
    <${Icon} name=${KEEP_ICON[st]} /><span class="kp-w">${KEEP_WORD[st]}</span><span class="kp-c">${col}</span></span>`;
}

// Why a step cannot run now (the gate, without the arm box): "" when it can.
function stepWhy(bid, s) {
  if (s.no) return s.no;
  return gateReason(bid, s.panel, s.key, { ...s.gate, arm: undefined });
}

function RebootPhases({ b }) {
  const phases = ["sent", "down", "up"];
  const at = phases.filter((p) => b.reboot.phases.includes(p)).length;
  return html`<div class="progress-box" data-testid="reboot-phases">
    <div class="meter-line"><span>Rebooting · ${b.reboot.phases[b.reboot.phases.length - 1] || "sent"}</span>
      <span class="num">${at} of 3</span></div>
    <div class="meter"><div style=${{ width: `${Math.round((at / 3) * 100)}%` }}></div></div></div>`;
}

function StepRow({ bid, s, leaseWhy }) {
  const b = boardState(bid);
  const p = panelState(bid, s.panel);
  const why = gateReason(bid, s.panel, s.key, s.gate);
  const running = p.running === s.key;
  const blocked = (!!why || !!s.no) && !running;
  const armed = !!b.arms[s.gate.arm];
  const off = !!stepWhy(bid, s) && !running;
  // Back to greybox is a deploy: its events draw the same phase bar as the Workbench's
  const bar = s.k === "greybox" && running ? deployBar(b.deploy) : null;
  const onClick = () => {
    if (running) return;
    if (s.no) { interlock(bid, s.panel, s.spec.command, s.no); return; }
    if (why) { interlock(bid, s.panel, s.spec.command, why); return; }
    runAction(bid, s.panel, { ...s.spec, arm: s.gate.arm });
  };
  // The reason under the row, in words: the lease reason is said once above the ladder and
  // "not armed" by the arm box itself, so those two stay for assistive tech only.
  const reason = running ? "" : s.no || why;
  const quiet = !!reason && (reason.startsWith("not armed") || (!!leaseWhy && reason.includes("for the lease holder only")));
  const label = running
    ? html`<${Spinner} /><span>${p.busyLabel}</span><span class="elapsed">${Math.floor(elapsedSince(p.startedAt))} s</span>`
    : html`<span>${s.bt}</span>`;
  return html`<li class=${`rc-row ${off ? "off" : ""} ${armed ? "armed" : ""}`} data-sev=${s.sev} data-step=${s.k}
      data-testid=${s.testid}>
    <span class="rc-rung" title=${`Step ${s.sev} of 5`}>${s.sev}</span>
    <div class="rc-main">
      <div class="rc-top"><span class="rc-t" title=${s.d}>${s.t}</span>
        <span class="rc-sev" title=${`Severity ${s.sev} of 5`}>${[1, 2, 3, 4, 5].map((n) => html`<i key=${n} class=${n <= s.sev ? "on" : ""}></i>`)}</span>
        <span class="chip plain rc-time" title="Typical time"><${Icon} name="timer" />${s.time}</span></div>
      <div class="rc-d" title=${s.d}>${s.sh}</div></div>
    <div class="rc-keeps" aria-label="What this step keeps">${KEEP_COLS.map(([c, lbl]) => html`<${KeepCell} key=${c} col=${lbl} v=${s.keep[c]} />`)}</div>
    <div class="rc-acts">
      <${ArmBox} bid=${bid} armKey=${s.gate.arm} compact=${true} testid=${s.arm} text=${ARM_TEXT[s.key] || `Arm: ${s.t}`} />
      <button type="button" class="btn sm rc-go" data-action=${s.key}
        aria-disabled=${blocked ? "true" : undefined} aria-busy=${running ? "true" : undefined}
        title=${blocked ? `${s.t}: ${s.no || why}` : s.t} onClick=${onClick}>${label}</button></div>
    ${reason || s.note || s.fix || (s.k === "power") || (s.k === "reboot" && running && b.reboot.phases.length) || (s.k === "greybox" && bar) || (p.lines && p.lines.length)
      ? html`<div class="rc-extra">
        ${reason ? html`<p class=${`reason ${quiet ? "sr-only" : reason.startsWith("Cannot") ? "unk" : s.no ? "" : "warn"}`}
            data-testid=${`reason-${s.key}`}><${Icon} name=${reason.startsWith("Cannot") ? "circle-slash" : "info"} />
          <span>${reason}${s.fix && reason.startsWith("Cannot") ? ` Fix: ${s.fix}` : ""}</span>
          ${s.fix && reason.startsWith("Cannot") ? html` <button type="button" class="link-btn" onClick=${() => navigate(bid, "board/connections")}>See Connections</button>` : null}</p>` : null}
        ${s.note && !reason ? html`<${Reason} text=${s.note} />` : null}
        ${s.k === "power" ? html`<div class="field"><label for=${`off-${bid}`}>Off for</label>
          <input class="input num sm" id=${`off-${bid}`} type="number" min=${OFF_S.min} max=${OFF_S.max} step="1"
            value=${b.powerOffS ?? OFF_S.fallback} data-testid="power-off-s"
            onInput=${(e) => { b.powerOffS = e.target.value; changed(); }} /><span class="muted small">seconds (${OFF_S.min} to ${OFF_S.max})</span></div>` : null}
        ${s.k === "reboot" && running && b.reboot.phases.length ? html`<${RebootPhases} b=${b} />` : null}
        ${s.k === "greybox" && bar ? html`<div class="progress-box" data-testid="greybox-progress"><div class="meter-line">
          <span>Swapping to ${bar.overlay || "greybox"} · ${bar.line}</span><span class="num">${bar.pct}</span></div><${MiniBar} bar=${bar} /></div>` : null}
        ${p.lines && p.lines.length ? html`<${ResultBlock} lines=${p.lines} panel=${p} testid=${s.result} />` : null}
      </div>` : null}
  </li>`;
}

export function RecoverPage({ bid }) {
  const b = boardState(bid);
  const w = week(bid);
  useEffect(() => { if (!w.power && !w.powerError) loadPower(bid); }, [bid]);
  // What the board boots from decides what Restart and Reboot say (a netboot board waits in rescue)
  useEffect(() => { if (isLinux(b) && !b.slots && !b.slotsLoading) loadSlots(bid); }, [bid, isLinux(b)]);
  const steps = recoverSteps(bid);
  const shown = steps.filter((s) => !s.hidden);
  const leaseWhy = driveWhy(bid, "Recovery");
  const guard = cardGuard(bid);
  const can = shown.filter((s) => !stepWhy(bid, s)).map((s) => s.sev);
  const cannot = shown.filter((s) => (s.no && s.k !== "greybox") || stepWhy(bid, s).startsWith("Cannot"));
  const power = steps.find((s) => s.k === "power");
  const running = b.job && !["lease", "lease_request", "lease_force"].includes(b.job.kind) ? b.job : null;
  return html`<${Card} title="Recover" icon="power" cls="rc" testid="recover-card" bodyCls="flush"
      sub="Gentle to heavy: start at the top and stop at the first step that brings the board back.">
    <div class="rc-head">
      ${leaseWhy ? html`<${Reason} icon="lock" testid="recover-lease" text=${`${leaseWhy}.`} />`
        : html`<${Reason} level=${cannot.length || power.hidden ? "" : "ok"} testid="recover-can"
            text=${`This board can do step${can.length === 1 ? "" : "s"} ${rangeText(can)} now${cannot.length ? `; not step${cannot.length === 1 ? "" : "s"} ${rangeText(cannot.map((s) => s.sev))} (the row says why)` : ""}.`} />`}
      ${power.hidden ? html`<${Reason} icon="circle-slash" testid="power-cycle-reason"
          text=${power.noText ? `Step 5, Power-cycle: cannot, ${power.noText}.`
            : w.powerUnsupported ? "Step 5, Power-cycle: this service has no power routes."
            : w.powerError ? `Step 5, Power-cycle: the supply could not be read (${w.powerError.errName}: ${w.powerError.message}).`
            : "Step 5, Power-cycle: reading the supply..."} />` : null}
      ${guard ? html`<${Reason} level="warn" testid="recover-guard" text=${`Restart, reboot and power-cycle wait: ${guard}`} />` : null}
      ${running ? html`<${Reason} icon="loader-circle" testid="recover-job" text=${`A ${jobLabel(running.kind)} job holds the board: every step waits for it.`} />` : null}
    </div>
    <div class="rc-cols" aria-hidden="true"><span></span><span class="rc-cols-step">Step<span class="rc-grad"></span>heavier</span>
      <span class="rc-cols-keep" title="What each step keeps">${KEEP_COLS.map(([c, l, sh]) => html`<span key=${c} title=${l}>${sh}</span>`)}</span><span></span></div>
    <ol class="rc-list">${shown.map((s) => html`<${StepRow} key=${s.k} bid=${bid} s=${s} leaseWhy=${leaseWhy} />`)}</ol>
    <div class="rc-legend"><span class="rc-key"><span class="kp kept"><${Icon} name="check" /><span>kept</span></span>
      <span class="kp back"><${Icon} name="refresh-cw" /><span>drops, then comes back</span></span>
      <span class="kp held"><${Icon} name="circle-pause" /><span>held in reset</span></span>
      <span class="kp lost"><${Icon} name="x" /><span>lost</span></span></span>
      <span>Hover a step or a cell for the detail. Your hub lease survives every step.</span></div>
  <//>`;
}

// The Recover page's status line in the side list: [text, dot, title].
export function recoverStatus(bid) {
  const b = boardState(bid);
  const steps = recoverSteps(bid).filter((s) => !s.hidden);
  const leaseWhy = driveWhy(bid, "Recovery");
  const busy = steps.find((s) => panelState(bid, s.panel).running === s.key);
  if (busy) return [`${busy.t.toLowerCase()}...`, "accent", `${busy.t} is running`];
  if (b.job && !["lease", "lease_request", "lease_force"].includes(b.job.kind)) {
    return [`waiting: ${jobLabel(b.job.kind)} job`, "accent", `A ${jobLabel(b.job.kind)} job holds the board`];
  }
  if (leaseWhy) return ["watch only: no lease", "held", leaseWhy];
  const can = steps.filter((s) => !stepWhy(bid, s)).map((s) => s.sev);
  const guard = cardGuard(bid);
  return [`steps ${rangeText(can)} of 5 here`, null, guard ? `Restart, reboot and power-cycle wait: ${guard}` : "The steps this board can do now"];
}

