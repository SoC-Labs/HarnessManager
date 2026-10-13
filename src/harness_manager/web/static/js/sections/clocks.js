// Board › Readings (UI v2 round 3, lane UI2-BOARD): what the board reports now and where each
// value comes from: health, how fast it answers and how long it has run (G4's answer_ms,
// uptime_s), every telemetry reading with its source (a value it cannot give says why, never
// 0), a metered supply when boards.toml has one, the DUT clock presets (GET/POST /clocks,
// the shell's `clock` verb), the board controller's oscillators (read-only, CFG R OSC) and
// the counters since the shell started.

import { panelState } from "../actions.js";
import { call, toApiError } from "../api.js";
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

// --- Sensors (mint 4, feature `sysmon`): GET/POST /boards/{bid}/sensors --------------------------
// The harness's converted SYSMON values. Setting thresholds or the calibration, and clearing the
// alarms, are claim-locked and armed. The ranges are net-protocol.md "sensors"; the board checks
// them again and applies nothing if any value is refused.

// SENSORS-VALIDATE-BEGIN
const SENSOR_FIELDS = [
  ["temp_hi_c", "Temperature alarm level", "degC"],
  ["temp_ot_c", "Over-temperature level", "degC"],
  ["vccint_lo_v", "VCCINT lower limit", "V"],
  ["vccint_hi_v", "VCCINT upper limit", "V"],
  ["k", "Calibration", ""],
];

// The values to send (only the boxes filled in) and the problems, in plain words. `ref` is the
// board's reference ("internal" or "external") and `thr` the thresholds now in force.
function checkSensorForm(form, ref, thr) {
  const out = {};
  const errors = [];
  for (const [key] of SENSOR_FIELDS) {
    const raw = form[key] === undefined || form[key] === null ? "" : String(form[key]).trim();
    if (raw === "") continue;
    const v = Number(raw);
    if (!Number.isFinite(v)) { errors.push(`${key}: "${raw}" is not a number`); continue; }
    out[key] = v;
  }
  if (!Object.keys(out).length) return { values: out, errors: ["Fill in at least one value."] };
  const has = (k) => out[k] !== undefined;
  const cur = (k) => (has(k) ? out[k] : (thr || {})[k]);
  for (const k of ["temp_hi_c", "temp_ot_c"]) {
    if (has(k) && !(out[k] > 0 && out[k] <= 125)) errors.push(`${k}: must be above 0 and at most 125 degC`);
  }
  if ((has("temp_hi_c") || has("temp_ot_c")) && typeof cur("temp_hi_c") === "number"
      && typeof cur("temp_ot_c") === "number" && !(cur("temp_hi_c") < cur("temp_ot_c"))) {
    errors.push("The temperature alarm level must be below the over-temperature level.");
  }
  for (const k of ["vccint_lo_v", "vccint_hi_v"]) {
    if (has(k) && !(out[k] >= 0.5 && out[k] <= 1.2)) errors.push(`${k}: must be between 0.5 and 1.2 V`);
  }
  if ((has("vccint_lo_v") || has("vccint_hi_v")) && typeof cur("vccint_lo_v") === "number"
      && typeof cur("vccint_hi_v") === "number" && !(cur("vccint_lo_v") < cur("vccint_hi_v"))) {
    errors.push("The VCCINT lower limit must be below its upper limit.");
  }
  if (has("k")) {
    const [lo, hi] = ref === "external" ? [0.5, 2.0] : [-20.0, 20.0];
    if (!(out.k >= lo && out.k <= hi)) {
      errors.push(`k: ${ref === "external" ? "the scale" : "the offset in degC"} must be between ${lo} and ${hi}`);
    }
  }
  return { values: out, errors };
}

const ALARM_BITS = ["Over temperature", "Temperature above its alarm level",
  "VCCINT outside its window", "VCCAUX outside its window", "VCCBRAM outside its window"];
function alarmWords(alm) {
  const n = Number(alm) || 0;
  return ALARM_BITS.filter((_, i) => (n >> i) & 1);
}
// SENSORS-VALIDATE-END

const sensorsByBoard = {};
function sensorsOf(bid) {
  if (!sensorsByBoard[bid]) sensorsByBoard[bid] = { data: null, error: null, loading: false, form: {} };
  return sensorsByBoard[bid];
}

async function loadSensors(bid) {
  const st = sensorsOf(bid);
  if (st.loading) return;
  st.loading = true;
  changed();
  try {
    st.data = (await call("sensors", { bid })).data.sensors;
    st.error = null;
  } catch (e) {
    st.error = toApiError(e);
  }
  st.loading = false;
  changed();
}

const fmt = (v, n, unit) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "n/a" : `${Number(v).toFixed(n)} ${unit}`);

function SensorsCard({ bid }) {
  const b = boardState(bid);
  const st = sensorsOf(bid);
  const cap = capState(b.info, "mps3.sensors");
  useEffect(() => { if (cap && cap.available) loadSensors(bid); }, [bid, cap && cap.available]);
  if (!cap || !cap.available) return null;           // a board without the sensors shows no card
  const d = st.data;
  const p = panelState(bid, "sensors");
  const check = checkSensorForm(st.form, d && d.ref, d && d.thr);
  const touched = Object.values(st.form).some((v) => String(v ?? "").trim() !== "");
  const armKey = "sensors";
  const finish = () => { st.form = {}; return loadSensors(bid); };
  const setSpec = {
    key: "sensors-set", label: "Set alarm thresholds / calibration", busyLabel: "Setting...", budgetS: 20,
    command: `sensors set ${Object.entries(check.values).map(([k, v]) => `${k}=${v}`).join(" ")}`.trim(),
    run: async () => {
      if (check.errors.length) throw new Error(check.errors[0]);
      return (await call("sensorsAct", { bid }, { act: "set", ...check.values })).data.sensors;
    },
    render: (r) => [{ kind: "ok", text: r && r.persisted === false
      ? "Set for this boot only (the board has no card-backed storage to keep it)." : "Set and kept." }],
    onDone: finish,
  };
  const clearSpec = {
    key: "sensors-clear", label: "Clear alarms", busyLabel: "Clearing...", budgetS: 20,
    command: "sensors clear_alm",
    run: async () => (await call("sensorsAct", { bid }, { act: "clear_alm" })).data.sensors,
    render: () => [{ kind: "ok", text: "Alarms cleared." }],
    onDone: finish,
  };
  const alarms = d ? alarmWords(d.alm) : [];
  const rails = d ? [["VCCINT", d.vccint_v, d.thr && `${d.thr.vccint_lo_v} to ${d.thr.vccint_hi_v} V`],
    ["VCCAUX", d.vccaux_v, ""], ["VCCBRAM", d.vccbram_v, ""]] : [];
  const unit = d && d.ref === "external" ? "scale" : "offset, degC";
  return html`<${Card} title="Sensors" icon="thermometer" testid="sensors-card"
      sub="Die temperature and supply rails, read by the harness. There is no power sensor on this board.">
    ${st.error ? html`<${Reason} level="err" testid="sensors-error"
      text=${st.error.errName === "UNAVAILABLE" ? `Cannot: ${st.error.reason || st.error.message}` : `${st.error.errName}: ${st.error.message}`} />` : null}
    ${!d && !st.error ? html`<p class="muted"><${Spinner} /> Reading...</p>` : null}
    ${d ? html`<table class="table" data-testid="sensors-table"><tbody>
      <tr data-reading="die-temp"><td>Die temperature</td>
        <td class="r num">${fmt(d.temp_c, 1, "°C")}
          <div class="small muted">${fmt(d.temp_min_c, 1, "°C")} lowest, ${fmt(d.temp_max_c, 1, "°C")} highest since power-up</div></td>
        <td class="small muted">${d.cal && d.cal !== "none" ? `Calibrated (${d.cal === "persist" ? "kept from before" : "set this boot"})` : "Uncalibrated"}</td></tr>
      ${rails.map(([name, v, win]) => html`<tr key=${name} data-reading=${name.toLowerCase()}><td>${name}</td>
        <td class="r num">${fmt(v, 3, "V")}</td>
        <td class="small muted">${win ? `Alarm window ${win}` : "Fixed alarm window"}</td></tr>`)}
      ${d.touch_temp_c !== null && d.touch_temp_c !== undefined ? html`<tr data-reading="lcd-ambient"><td>LCD module ambient</td>
        <td class="r num">${fmt(d.touch_temp_c, 1, "°C")}</td><td class="small muted">Not the FPGA die</td></tr>` : null}
      <tr data-testid="sensors-alarms"><td>Alarms</td>
        <td colspan="2">${alarms.length ? alarms.map((w) => html`<${Chip} key=${w} level="err" icon="circle-alert">${w}<//> `)
          : html`<span class="muted">None since the last clear</span>`}</td></tr>
      <tr><td>Alarm levels now</td><td colspan="2" class="small muted">${d.thr ? `Temperature ${d.thr.temp_hi_c} °C, over-temperature ${d.thr.temp_ot_c} °C, VCCINT ${d.thr.vccint_lo_v} to ${d.thr.vccint_hi_v} V` : ""}</td></tr>
    </tbody></table>
    <div class="actions" data-testid="sensors-form">
      <div class="row wrap">${SENSOR_FIELDS.map(([key, label, u]) => html`<label key=${key} class="field small">
        <span>${label}${key === "k" ? ` (${unit})` : u ? ` (${u})` : ""}</span>
        <input type="text" inputmode="decimal" data-field=${key} value=${st.form[key] ?? ""}
          placeholder=${key === "k" ? String(d.k) : String((d.thr || {})[key] ?? "")}
          onInput=${(e) => { st.form[key] = e.target.value; changed(); }} /></label>`)}</div>
      ${touched && check.errors.length ? check.errors.map((m) => html`<${Reason} key=${m} level="err" text=${m} testid="sensors-invalid" />`) : null}
      <${ActionRow} bid=${bid} panel="sensors" spec=${setSpec} variant="primary" icon="shield-check" compact=${true}
        gate=${{ arm: armKey, holder: "Changing the sensor settings", capability: "mps3.sensors" }}>
        <${ArmBox} bid=${bid} armKey=${armKey} testid="arm-sensors" compact=${true}
          text="Arm: I understand this changes the board's alarm settings; they are kept across restarts when the board has a card." /><//>
      <${ActionRow} bid=${bid} panel="sensors" spec=${clearSpec} icon="circle-check" compact=${true}
        gate=${{ arm: armKey, holder: "Clearing the sensor alarms", capability: "mps3.sensors" }} />
      <${ResultBlock} lines=${p.lines} panel=${p} testid="sensors-result" />
    </div>` : null}
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
      <${SensorsCard} bid=${bid} />
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
