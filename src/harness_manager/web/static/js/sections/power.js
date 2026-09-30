// Power: the supply (readings and a cold power cycle through a networked outlet), DUT
// reset, shell restart and board reboot. Every intrusive action is armed.
//
// The action specs are exported: the Overview's Board tile runs the same actions, with the
// same panels and arm boxes, so a result shows in both places.

import { panelState, runJob } from "../actions.js";
import { call } from "../api.js";
import { ageText, capState, valueText } from "../format.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed, scheduleRefresh } from "../store.js";
import { loadPower, week } from "../week.js";
import { ActionRow, ArmBox, Card, Reason, ResultBlock, Spinner } from "../ui.js";

// The engine picks the reboot wait by harness implementation (bare-metal ~120 s, Linux
// 300 s: constants.reboot_wait_s), so the UI sends none and budgets for the longer one.
const REBOOT_BUDGET_S = 330;

export const ARM_TEXT = {
  reset_dut: "Arm: I understand this resets the DUT CPU (the harness keeps running).",
  reboot: "Arm: I understand this restarts the whole board; every session, console and loaded overlay is lost.",
  power_cycle: "Arm: I understand this cuts the board's power; everything on it stops and it boots from its SD.",
};

export function resetTargets(bid) {
  const s = boardState(bid).session;
  return s && Array.isArray(s.reset_targets) ? s.reset_targets : null;   // null: not read
}

export function resetDutSpec(bid, target = "dut") {
  return {
    key: "reset_dut", label: "Reset DUT", busyLabel: "Resetting...", budgetS: 20,
    command: `reset ${target}`,
    run: async () => { await call("reset", { bid }, { target }); return `reset ${target}: done`; },
    render: (t) => [{ kind: "ok", text: t }],
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

// --- the supply ---------------------------------------------------------------------------

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
    onDone: () => { loadPower(bid); scheduleRefresh(bid, 200); },
  };
}

// Three rows that are all unavailable for one reason (no meter) say it once, not three times.
function sharedReason(readings) {
  if (!readings.length || readings.some((r) => r.value !== null && r.value !== undefined)) return "";
  const reasons = new Set(readings.map((r) => r.reason || ""));
  return reasons.size === 1 ? [...reasons][0] : "";
}

function PowerSupplyCard({ bid }) {
  const b = boardState(bid);
  const w = week(bid);
  const p = panelState(bid, "power_cycle");
  useEffect(() => { loadPower(bid); }, [bid]);
  const cap = capState(b.info, "power_cycle");
  const pw = w.power;
  const refresh = html`<button type="button" class="btn ghost sm" onClick=${() => loadPower(bid)}>Read again</button>`;
  if (w.powerUnsupported) {
    // This daemon has no power routes yet (lane L4): say what the board could do.
    return html`<${Card} title="Power supply" icon="plug-zap" testid="power-card"
        sub="A networked outlet or meter on the board's supply (boards.toml).">
      <${Reason} icon="circle-slash" testid="power-cycle-reason"
        text=${cap && !cap.available ? `Cannot: ${cap.reason}` : "This harness-manager-daemon has no power endpoints yet."} />
    <//>`;
  }
  const reason = pw ? pw.cycle_reason : "";
  const readings = (pw && pw.readings) || [];
  const shared = sharedReason(readings);
  const phases = ["off", "on"];
  return html`<${Card} title="Power supply" icon="plug-zap" actions=${refresh} testid="power-card"
      sub=${pw && pw.device ? `Through ${pw.device}.` : "A networked outlet or meter on the board's supply (boards.toml)."}>
    <div class="actions">
      ${w.powerError ? html`<${Reason} level="err" text=${`${w.powerError.errName}: ${w.powerError.message}`} />` : null}
      ${!pw && !w.powerError ? html`<p class="muted"><${Spinner} /> Reading the supply...</p>` : null}
      ${pw ? html`<table class="table readings" data-testid="power-readings">
        <tbody>${readings.map((r) => {
          const ok = r.value !== null && r.value !== undefined;
          return html`<tr key=${r.name} data-reading=${r.name}>
            <td><span class="mono">${r.name}</span>${!ok && r.reason && !shared ? html`<${Reason} text=${r.reason} icon="circle-slash" />` : null}</td>
            <td class="r num nowrap">${ok ? valueText(r) : html`<span class="muted">unavailable</span>`}</td>
            <td class="muted nowrap small">${ok && r.observed_at ? ageText(r.observed_at) : ""}</td></tr>`;
        })}</tbody></table>` : null}
      ${shared ? html`<${Reason} icon="circle-slash" testid="power-readings-reason" text=${`Readings: ${shared}`} />` : null}
      ${pw && !shared && readings[0] && readings[0].reason && readings[0].value !== null
        ? html`<p class="muted small">${readings[0].reason}</p>` : null}
      ${pw && reason ? html`<${Reason} icon="circle-slash" testid="power-cycle-reason" text=${`Power cycle: cannot, ${reason}`} />` : null}
      ${pw && !reason ? html`
        <div class="field"><label for=${`off-${bid}`}>Off for</label>
          <input class="input num sm" id=${`off-${bid}`} type="number" min=${OFF_S.min} max=${OFF_S.max} step="1"
            value=${b.powerOffS ?? OFF_S.fallback} data-testid="power-off-s"
            onInput=${(e) => { b.powerOffS = e.target.value; changed(); }} /><span class="muted small">seconds (${OFF_S.min} to ${OFF_S.max})</span></div>
        <${ArmBox} bid=${bid} armKey="power_cycle" testid="arm-power" text=${ARM_TEXT.power_cycle} />
        <${ActionRow} bid=${bid} panel="power_cycle" spec=${powerCycleSpec(bid, pw.device)} variant="danger" icon="power"
          gate=${{ arm: "power_cycle", guard: () => offGuard(b), holder: "Power-cycle" }} />
        ${w.powerPhases.length ? html`<div class="steps">${phases.map((ph) => html`<div key=${ph}
          class=${`step ${w.powerPhases.includes(ph) ? "done" : ""}`}><div class="bar"></div><span>outlet ${ph}</span></div>`)}</div>` : null}
        <${ResultBlock} lines=${p.lines} panel=${p} testid="power-result" />` : null}
    </div>
  <//>`;
}

// --- resets and reboot ---------------------------------------------------------------------

function DutResetCard({ bid }) {
  const p = panelState(bid, "reset_dut");
  const b = boardState(bid);
  // "shell" has its own card, gated on its own capability.
  const targets = (resetTargets(bid) || ["dut"]).filter((t) => t !== "shell");
  const target = targets.includes(b.resetTarget) ? b.resetTarget : targets[0] || "dut";
  return html`<${Card} title="DUT reset" icon="rotate-ccw" testid="reset-dut"
      sub="Pulses the DUT reset through the harness. The consoles stay up.">
    <div class="actions">
      ${targets.length > 1 ? html`<div class="field"><label for=${`rt-${bid}`}>Target</label>
        <select class="select" id=${`rt-${bid}`} value=${target}
          onChange=${(e) => { b.resetTarget = e.target.value; changed(); }}>
          ${targets.map((t) => html`<option key=${t} value=${t}>${t}</option>`)}</select></div>` : null}
      <${ArmBox} bid=${bid} armKey="reset_dut" text=${ARM_TEXT.reset_dut} />
      <${ActionRow} bid=${bid} panel="reset_dut" spec=${resetDutSpec(bid, target)} icon="rotate-ccw"
        gate=${RESET_DUT_GATE} />
      <${ResultBlock} lines=${p.lines} panel=${p} />
    </div>
  <//>`;
}

function ShellRestartCard({ bid }) {
  const p = panelState(bid, "reset_shell");
  const spec = {
    key: "reset_shell", label: "Restart shell", busyLabel: "Restarting...", budgetS: 60, command: "reset shell",
    run: async () => { await call("reset", { bid }, { target: "shell" }); return "reset shell: done"; },
    render: (t) => [{ kind: "ok", text: t }],
    onDone: () => scheduleRefresh(bid, 500),
  };
  return html`<${Card} title="Restart the shell" icon="refresh-cw" testid="reset-shell"
      sub="Restarts the harness firmware without reloading the FPGA.">
    <div class="actions">
      <${ArmBox} bid=${bid} armKey="reset_shell" text="Arm: I understand every console and the debug session drop while the shell restarts." />
      <${ActionRow} bid=${bid} panel="reset_shell" spec=${spec} icon="refresh-cw"
        gate=${{ capability: "reset_shell", adapter: "resets", arm: "reset_shell", holder: "Restart shell",
          guard: () => {
            const t = resetTargets(bid);
            return t && !t.includes("shell") ? "Cannot: this board's reset adapter offers no 'shell' target" : "";
          } }} />
      <${ResultBlock} lines=${p.lines} panel=${p} />
    </div>
  <//>`;
}

function RebootCard({ bid }) {
  const b = boardState(bid);
  const p = panelState(bid, "reboot");
  const phases = ["sent", "down", "up"];
  return html`<${Card} title="Board reboot" icon="power" testid="reboot-card"
      sub="Reboots through the board controller and proves it: the board went down, then came back. The board then runs what is on its SD card.">
    <div class="actions">
      <${ArmBox} bid=${bid} armKey="reboot" testid="arm-reboot" text=${ARM_TEXT.reboot} />
      <${ActionRow} bid=${bid} panel="reboot" spec=${rebootSpec(bid)} variant="danger" icon="power"
        gate=${REBOOT_GATE} />
      ${b.reboot.phases.length ? html`<div class="steps">${phases.map((ph) => html`<div key=${ph}
        class=${`step ${b.reboot.phases.includes(ph) ? "done" : ""}`}><div class="bar"></div><span>${ph}</span></div>`)}</div>` : null}
      <${ResultBlock} lines=${p.lines} panel=${p} testid="reboot-result" />
    </div>
  <//>`;
}

export function PowerSection({ bid }) {
  return html`<div class="stack">
    <div class="grid two">
      <${PowerSupplyCard} bid=${bid} />
      <${RebootCard} bid=${bid} />
    </div>
    <div class="grid two">
      <${DutResetCard} bid=${bid} />
      <${ShellRestartCard} bid=${bid} />
    </div>
  </div>`;
}
