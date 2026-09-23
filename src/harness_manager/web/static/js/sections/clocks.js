// Clocks: the DUT clock presets through GET/POST /clocks (the shell's `clock` verb), and the
// board controller's oscillators, read-only (the MCC's CFG R OSC).

import { panelState } from "../actions.js";
import { call } from "../api.js";
import { ageText, capState, valueText } from "../format.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed } from "../store.js";
import { loadClocks, loadOsc, week } from "../week.js";
import { ActionRow, ArmBox, Card, Reason, ResultBlock, Seg, Spinner } from "../ui.js";

export const DUT_PRESETS_MHZ = [25, 50, 100];

function ReadingsTable({ readings, testid }) {
  return html`<table class="table readings" data-testid=${testid}>
    <tbody>${readings.map((r) => {
      const ok = r.value !== null && r.value !== undefined;
      return html`<tr key=${r.name} data-reading=${r.name}>
        <td><span class="mono">${r.name}</span>
          ${r.reason ? html`<${Reason} text=${r.reason} icon=${ok ? "info" : "circle-slash"} />` : null}</td>
        <td class="r num nowrap">${ok ? valueText(r) : html`<span class="muted">unavailable</span>`}</td>
        <td class="mono secondary small">${r.source || ""}</td>
        <td class="muted nowrap small">${ok && r.observed_at ? ageText(r.observed_at) : ""}</td></tr>`;
    })}</tbody></table>`;
}

function DutClockCard({ bid }) {
  const b = boardState(bid);
  const w = week(bid);
  const p = panelState(bid, "clock");
  useEffect(() => { loadClocks(bid); }, [bid]);
  const current = (w.clocks || []).find((r) => r.name === "dut");
  const known = current && current.value !== null && current.value !== undefined ? Number(current.value) : null;
  const want = b.clockWant ?? known ?? 50;
  const spec = {
    key: "clock", label: `Set ${want} MHz`, busyLabel: "Setting...", budgetS: 20,
    command: `clock dut ${want}`,
    run: async () => (await call("clockSet", { bid }, { name: "dut", mhz: want })).data.reading,
    render: (r) => [{ kind: "ok", text: r && r.value !== null ? `dut now ${valueText(r)} (${r.source || "shell"})` : "set" }],
    onDone: () => loadClocks(bid),
  };
  const cap = capState(b.info, "clock_dut");
  return html`<${Card} title="DUT clock" icon="clock" testid="clock-card"
      sub="The clock the DUT runs from, set through the harness. Presets only: 25, 50 or 100 MHz.">
    <div class="actions">
      ${w.clocksError ? html`<${Reason} icon="circle-slash" testid="clock-reason"
        text=${w.clocksError.errName === "UNAVAILABLE" ? `Cannot: ${w.clocksError.reason || w.clocksError.message}` : `${w.clocksError.errName}: ${w.clocksError.message}`} />` : null}
      ${!w.clocks && !w.clocksError ? html`<p class="muted"><${Spinner} /> Reading...</p>` : null}
      ${w.clocks ? html`<${ReadingsTable} readings=${w.clocks} testid="clock-readings" />` : null}
      ${w.clocks ? html`
        <div class="field"><label>Preset</label>
          <${Seg} label="DUT clock preset" value=${want}
            onChange=${(v) => { b.clockWant = v; changed(); }}
            options=${DUT_PRESETS_MHZ.map((m) => ({ value: m, label: `${m} MHz` }))} /></div>
        <${ArmBox} bid=${bid} armKey="clock" testid="arm-clock"
          text="Arm: I understand changing the DUT clock under a running program can upset it." />
        <${ActionRow} bid=${bid} panel="clock" spec=${spec} variant="primary" icon="clock"
          gate=${{ capability: cap ? "clock_dut" : undefined, arm: "clock" }} />
        <${ResultBlock} lines=${p.lines} panel=${p} testid="clock-result" />` : null}
    </div>
  <//>`;
}

function OscillatorsCard({ bid }) {
  const w = week(bid);
  useEffect(() => { loadOsc(bid); }, [bid]);
  const cap = capState(boardState(bid).info, "clock_board");
  return html`<${Card} title="Board oscillators" icon="activity" testid="osc-card"
      sub="The board controller's oscillator set-points, read-only here (CFG R OSC over the MCC console).">
    ${w.oscError ? html`<${Reason} icon="circle-slash" testid="osc-reason"
      text=${cap && !cap.available ? `Cannot: ${cap.reason}` : `${w.oscError.errName}: ${w.oscError.message}`} />` : null}
    ${!w.osc && !w.oscError ? html`<p class="muted"><${Spinner} /> Reading...</p>` : null}
    ${w.osc && w.osc.length ? html`<${ReadingsTable} readings=${w.osc} testid="osc-readings" />` : null}
    ${w.osc && !w.osc.length ? html`<p class="muted">The board controller reports no oscillators.</p>` : null}
  <//>`;
}

export function ClocksSection({ bid }) {
  return html`<div class="grid two">
    <${DutClockCard} bid=${bid} />
    <${OscillatorsCard} bid=${bid} />
  </div>`;
}
