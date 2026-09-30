// Board › Readings (UI v2 round 3, lane UI2-BOARD): what the board reports now and where each
// value comes from: health, how fast it answers and how long it has run (G4's answer_ms,
// uptime_s), every telemetry reading with its source (a value it cannot give says why, never
// 0), a metered supply when boards.toml has one, the DUT clock presets (GET/POST /clocks,
// the shell's `clock` verb), the board controller's oscillators (read-only, CFG R OSC) and
// the counters since the shell started.

import { panelState } from "../actions.js";
import { call } from "../api.js";
import { ageText, capState, clock, healthOf, valueText } from "../format.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed, loadTelemetry, quietWords, rereadBoard } from "../store.js";
import { loadClocks, loadOsc, loadPower, week } from "../week.js";
import { ActionRow, ArmBox, Card, Chip, Icon, QuietNote, Reason, ResultBlock, Seg, Spinner } from "../ui.js";
import { isLinux, uptimeText } from "./boardfacts.js";

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

// A power reading nobody can take (no meter on the MPS3) is left out: the page never shows a
// "not measured" supply (UI_V2_PLAN.md F4). A metered supply (boards.toml) shows its values.
function isSupply(r) { return ["W", "V", "A", "mV"].includes(r.unit); }

function upText(s, at) {
  if (s === null || s === undefined) return "";
  const since = at ? Math.max(0, Date.now() / 1000 - at) : 0;
  return uptimeText(Number(s) + since);
}

function Row({ label, value, source, testid = "", reading = "", available = true, reason = "" }) {
  return html`<tr data-testid=${testid || undefined} data-reading=${reading || undefined}
      data-available=${reading ? (available ? "yes" : "no") : undefined}>
    <td>${label}${reason ? html`<div class="small muted">${reason}</div>` : null}</td>
    <td class="r num">${value}</td><td class="mono secondary small">${source}</td></tr>`;
}

function ReadingsCard({ bid }) {
  const b = boardState(bid);
  const w = week(bid);
  const info = b.info;
  const health = healthOf(info);
  const tel = b.telemetry || [];
  const shown = tel.filter((r) => !(isSupply(r) && (r.value === null || r.value === undefined)));
  const supply = ((w.power && w.power.readings) || []).filter((r) => r.value !== null && r.value !== undefined);
  const again = html`<button type="button" class="btn ghost sm" data-action="readings-again" aria-busy=${b.infoLoading || b.telemetryLoading ? "true" : undefined}
    onClick=${() => { rereadBoard(bid); loadTelemetry(bid); loadPower(bid); }}>
    ${b.infoLoading || b.telemetryLoading ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`} Read again</button>`;
  const src = (info && info.readings_source) || "";
  return html`<${Card} title="Readings" icon="thermometer" testid="telemetry-card" bodyCls="flush" actions=${again}
      sub="What the board reports now, and where each value comes from. A value it cannot give says why, never 0.">
    ${b.telemetryError ? html`<div class="pad-x"><${Reason} level="err"
        text=${`${b.telemetryError.errName}: ${b.telemetryError.message}${b.telemetry ? " (showing the last good read)" : ""}`} /></div>` : null}
    ${!b.telemetry && b.telemetryQuiet ? html`<div class="pad-x"><${QuietNote} testid="telemetry-quiet" action="telemetry-read-now"
        text=${quietWords(b.telemetryQuiet)} busy=${b.telemetryLoading} onRead=${() => loadTelemetry(bid)} /></div>` : null}
    <table class="table" data-testid="telemetry-table" data-telemetry="yes">
      <colgroup><col style="width:36%" /><col style="width:28%" /><col style="width:36%" /></colgroup>
      <thead><tr><th>Reading</th><th class="r">Value</th><th>Source</th></tr></thead>
      <tbody>
        <${Row} label="Health" testid="reading-health" source=${(info && info.health && info.health.control_channel) ? `control channel ${info.health.control_channel}` : ""}
          value=${html`<${Chip} level=${health.level} icon=${health.level === "ok" ? "circle-check" : "circle-alert"}>${health.text}<//>`}
          reason=${b.infoError ? `the last read failed ${b.infoErrorAt ? clock(b.infoErrorAt) : ""}: ${b.infoError.message}` : ""} />
        ${info && info.answer_ms !== null && info.answer_ms !== undefined ? html`<${Row} label="Answers in" testid="reading-answer"
          value=${`${Number(info.answer_ms) < 1000 ? `${info.answer_ms} ms` : `${(info.answer_ms / 1000).toFixed(1)} s`}`} source="this service, around the read" />` : null}
        ${info && info.uptime_s !== null && info.uptime_s !== undefined ? html`<${Row} label=${isLinux(b) ? "Harness up (harnessd)" : "Harness up (the shell)"}
          testid="reading-uptime" value=${upText(info.uptime_s, info.readings_at)} source=${src} />` : null}
        ${info && info.os_uptime_s !== null && info.os_uptime_s !== undefined ? html`<${Row} label="Linux up" testid="reading-os-uptime"
          value=${upText(info.os_uptime_s, info.readings_at)} source=${src} />` : null}
        ${shown.map((r) => {
          const ok = r.value !== null && r.value !== undefined;
          return html`<${Row} key=${r.name} reading=${r.name} available=${ok} label=${html`<span class="mono">${r.name}</span>`}
            value=${ok ? html`${valueText(r)}${r.observed_at ? html`<div class="small muted">${ageText(r.observed_at)}</div>` : null}` : html`<span class="muted">unavailable</span>`}
            source=${r.source || "none given"} reason=${r.reason && !(ok && r.reason === "preset") ? r.reason : ""} />`;
        })}
        ${supply.map((r) => html`<${Row} key=${`p-${r.name}`} reading=${r.name} label=${html`<span class="mono">${r.name}</span>`}
          value=${valueText(r)} source=${(w.power && w.power.device) || r.source || "power meter"} />`)}
      </tbody></table>
    ${!b.telemetry && !b.telemetryQuiet ? html`<p class="muted pad-x small">${b.telemetryLoading ? "Reading the telemetry..." : "Telemetry not read yet."}</p>` : null}
    ${b.telemetryLine ? html`<p class="muted mono pad-x pad-y small">${b.telemetryLine}</p>` : null}
  <//>`;
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
      sub="The DUT's clock, from the shell's clock adapter. Presets only: 25, 50 or 100 MHz.">
    <div class="actions">
      ${w.clocksError ? html`<${Reason} icon="circle-slash" testid="clock-reason"
        text=${w.clocksError.errName === "UNAVAILABLE" ? `Cannot: ${w.clocksError.reason || w.clocksError.message}` : `${w.clocksError.errName}: ${w.clocksError.message}`} />` : null}
      ${!w.clocks && !w.clocksError ? html`<p class="muted"><${Spinner} /> Reading...</p>` : null}
      ${w.clocks ? html`<${ReadingsTable} readings=${w.clocks} testid="clock-readings" />` : null}
      ${w.clocks ? html`
        <div class="row"><${Seg} label="DUT clock preset" value=${want}
            onChange=${(v) => { b.clockWant = v; changed(); }}
            options=${DUT_PRESETS_MHZ.map((m) => ({ value: m, label: `${m} MHz` }))} />
          <span class="small muted">nanosoc firmware is built for 50 MHz</span></div>
        <${ActionRow} bid=${bid} panel="clock" spec=${spec} variant="primary" icon="clock" compact=${true}
          gate=${{ capability: cap ? "clock_dut" : undefined, arm: "clock", holder: "Setting the DUT clock" }}>
          <${ArmBox} bid=${bid} armKey="clock" testid="arm-clock" compact=${true}
            text="Arm: I understand changing the DUT clock under a running program can upset it." /><//>
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
    ${w.osc && !w.osc.length ? html`<p class="muted small">The board controller reports no oscillators.</p>` : null}
  <//>`;
}

const STATS_SHOWN = ["swap_n", "icap", "rxdrop", "txerr", "svc_max_us", "svc_max_ix", "svc_overruns", "svc_skips"];

function CountersCard({ bid }) {
  const b = boardState(bid);
  const counters = Object.entries((b.info && b.info.health && b.info.health.counters) || {});
  const stats = (b.info && b.info.stats) || {};
  const extra = STATS_SHOWN.filter((k) => stats[k] !== undefined && stats[k] !== null).map((k) => [k, stats[k]]);
  const num = (v) => (typeof v === "number" ? v.toLocaleString("en-GB") : String(v));
  return html`<${Card} title="Counters" icon="hash" testid="health-card" sub="Since the shell started.">
    ${counters.length || extra.length ? html`<div class="tags" data-testid="counters">
        ${counters.map(([k, v]) => html`<span class="tag" key=${k} data-counter=${k}>${k} ${num(v)}</span>`)}
        ${extra.map(([k, v]) => html`<span class="tag" key=${`s-${k}`} data-counter=${k} title="from the harness's stats">${k} ${num(v)}</span>`)}</div>`
      : html`<p class="muted small">The harness reports no counters.</p>`}
    ${((b.info && b.info.health && b.info.health.notes) || []).map((n) => html`<div class="mt-8" key=${n}><${Reason} text=${n} testid="health-note" /></div>`)}
  <//>`;
}

export function ReadingsPage({ bid }) {
  const b = boardState(bid);
  const w = week(bid);
  useEffect(() => {
    if (!b.telemetry && !b.telemetryLoading) loadTelemetry(bid, { background: true });
    if (!w.power && !w.powerError) loadPower(bid);
  }, [bid]);
  return html`<div class="bt-pair">
    <${ReadingsCard} bid=${bid} />
    <div class="bt-stack">
      <${DutClockCard} bid=${bid} />
      <${OscillatorsCard} bid=${bid} />
      <${CountersCard} bid=${bid} />
    </div>
  </div>`;
}

// The Readings page's status line in the side list: [text, dot, title].
export function readingsStatus(bid) {
  const b = boardState(bid);
  if (b.infoError && b.info) return ["stale · the last read failed", "warn", `${b.infoError.errName}: ${b.infoError.message}`];
  if (!b.info) return [b.infoLoading ? "reading..." : "not read yet", null, "The board has not been read yet"];
  const tel = b.telemetry || [];
  const temp = tel.find((r) => r.unit === "degC" && r.value !== null && r.value !== undefined);
  const dut = tel.find((r) => r.name === "dut_clk" && r.value !== null && r.value !== undefined);
  const h = healthOf(b.info);
  const bits = [temp ? `${Number(temp.value).toFixed(1)} °C` : "", dut ? `DUT ${valueText(dut)}` : ""].filter(Boolean);
  return [bits.length ? bits.join(" · ") : h.text, h.level === "err" ? "err" : h.level === "warn" ? "warn" : null,
    `${h.text}${b.infoOkAt ? ` · last read ${clock(b.infoOkAt)}` : ""}`];
}
