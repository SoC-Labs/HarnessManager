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
// 180 s: constants.reboot_wait_s), so the UI sends none and budgets for the longer one.
const REBOOT_BUDGET_S = 210;

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

export const RESET_DUT_GATE = { capability: "reset_dut", adapter: "resets", arm: "reset_dut" };

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

export const REBOOT_GATE = { capability: "reboot_board", adapter: "controller", arm: "reboot" };

// The reboot job's result is the controller's evidence: {summary, down_after_s, up_after_s,
// down_evidence[], up_evidence, shell_id_before, shell_id_after, fpga_configured}.
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
  for (const line of [].concat(ev.down_evidence || [])) out.push({ kind: "hint", text: `down: ${line}` });
  if (ev.up_evidence) out.push({ kind: "hint", text: `up: ${typeof ev.up_evidence === "object" ? JSON.stringify(ev.up_evidence) : ev.up_evidence}` });
  return out;
}

// --- the supply ---------------------------------------------------------------------------

function powerCycleSpec(bid, device) {
  return {
    key: "power_cycle", label: "Power-cycle", busyLabel: "Cycling...", budgetS: 240,
    command: `power cycle${device ? ` (${device})` : ""}`,
    run: (ctx) => runJob("powerCycle", { bid }, {}, (d) => ctx.progress(`power: ${d.phase}`, d.phase), "power_cycle"),
    render: (ev) => (ev && typeof ev === "object"
      ? [{ kind: "ok", text: ev.summary || "the board was power-cycled" }]
      : [{ kind: "ok", text: "the board was power-cycled" }]),
    onDone: () => { loadPower(bid); scheduleRefresh(bid, 200); },
  };
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
  const phases = ["off", "on", "up"];
  return html`<${Card} title="Power supply" icon="plug-zap" actions=${refresh} testid="power-card"
      sub=${pw && pw.device ? `Through ${pw.device}.` : "A networked outlet or meter on the board's supply (boards.toml)."}>
    <div class="actions">
      ${w.powerError ? html`<${Reason} level="err" text=${`${w.powerError.errName}: ${w.powerError.message}`} />` : null}
      ${!pw && !w.powerError ? html`<p class="muted"><${Spinner} /> Reading the supply...</p>` : null}
      ${pw ? html`<table class="table readings" data-testid="power-readings">
        <tbody>${(pw.readings || []).map((r) => {
          const ok = r.value !== null && r.value !== undefined;
          return html`<tr key=${r.name} data-reading=${r.name}>
            <td><span class="mono">${r.name}</span>${!ok && r.reason ? html`<${Reason} text=${r.reason} icon="circle-slash" />` : null}</td>
            <td class="r num nowrap">${ok ? valueText(r) : html`<span class="muted">unavailable</span>`}</td>
            <td class="muted nowrap small">${ok && r.observed_at ? ageText(r.observed_at) : ""}</td></tr>`;
        })}</tbody></table>` : null}
      ${pw && reason ? html`<${Reason} icon="circle-slash" testid="power-cycle-reason" text=${`Power cycle: cannot, ${reason}`} />` : null}
      ${pw && !reason ? html`
        <${ArmBox} bid=${bid} armKey="power_cycle" testid="arm-power" text=${ARM_TEXT.power_cycle} />
        <${ActionRow} bid=${bid} panel="power_cycle" spec=${powerCycleSpec(bid, pw.device)} variant="danger" icon="power"
          gate=${{ arm: "power_cycle" }} />
        ${w.powerPhases.length ? html`<div class="steps">${phases.map((ph) => html`<div key=${ph}
          class=${`step ${w.powerPhases.includes(ph) ? "done" : ""}`}><div class="bar"></div><span>${ph}</span></div>`)}</div>` : null}
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
        gate=${{ capability: "reset_shell", adapter: "resets", arm: "reset_shell",
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
