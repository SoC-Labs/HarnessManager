// Overview: identity, health, telemetry (a source per value), capabilities (with reasons).

import { ageText, CAPABILITY_ORDER, capTitle, clock, healthOf, valueText } from "../format.js";
import { html, useEffect, useState } from "../lib.js";
import { boardState, loadTelemetry, refreshInfo } from "../store.js";
import { Card, CheckChip, Chip, Icon, LinkLine, Reason, Spinner } from "../ui.js";

function IdentityCard({ bid, info }) {
  const b = boardState(bid);
  const cand = info.candidate || {};
  const id = info.identity || {};
  const build = id.build_check || "unchecked";
  const buildNote = {
    ok: "The harness firmware matches the fabric it runs on.",
    mismatch: "The harness firmware was built for different fabric. Treat results with care.",
    unchecked: "The harness could not compare its firmware with the fabric. Unchecked is not a pass.",
  }[build] || "";
  const refresh = html`<button type="button" class="btn ghost sm" onClick=${() => refreshInfo(bid)}
    aria-busy=${b.infoLoading ? "true" : undefined} title="Read the board again">
    ${b.infoLoading ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`} Refresh</button>`;
  return html`<${Card} title="Identity" icon="cpu" actions=${refresh} testid="identity-card">
    <dl class="kv">
      <dt>Board</dt>
      <dd>${cand.label || "unnamed board"}
        <div class="sub mono">${cand.board_id}</div>
        ${cand.evidence ? html`<div class="sub">found: ${cand.evidence}</div>` : null}</dd>
      <dt>Links</dt>
      <dd>${(cand.links || []).length ? (cand.links || []).map((l) => html`<${LinkLine} key=${l.kind + l.address} link=${l} />`)
        : html`<span class="muted">none</span>`}</dd>
      <dt>Shell</dt><dd class="mono" data-testid="id-shell">${id.shell_id || "unknown"}</dd>
      <dt>Design</dt>
      <dd>${id.rm_name || "unknown design"} <span class="mono sub">${id.rm_id ? `rm_id ${id.rm_id}` : ""}</span></dd>
      <dt>Harness</dt>
      <dd><div class="line" data-testid="id-harness">${id.harness_version || "unknown"}
        ${id.harness_impl ? html`<span class="tag">${id.harness_impl}</span>`
          : html`<span class="sub" title="the harness predates the version verb, so it cannot say">implementation unknown</span>`}
        ${id.proto ? html`<span class="sub">protocol ${id.proto}</span>` : null}</div>
        <div class="sub">firmware <span class="mono">${id.firmware_sha || "?"}</span>
          ${id.firmware_dirty ? html` <${Chip} level="warn" icon="triangle-alert">dirty build<//>` : null}</div></dd>
      <dt>Build check</dt>
      <dd><div class="line"><${CheckChip} check=${build} testid="id-build" /></div>
        <${Reason} text=${buildNote} level=${build === "ok" ? "" : build === "mismatch" ? "err" : "unk"} testid="id-build-note" /></dd>
      <dt>Features</dt>
      <dd>${(id.features || []).length ? html`<div class="tags">${id.features.map((f) => html`<span class="tag" key=${f}>${f}</span>`)}</div>`
        : html`<span class="muted">none reported</span>`}</dd>
      <dt>Unit id</dt><dd>${id.unit_id ? html`<span class="mono">${id.unit_id}</span>` : html`<span class="muted">not reported by this harness</span>`}</dd>
      ${id.usercode ? html`<dt>Usercode</dt><dd class="mono">${id.usercode}</dd>` : null}
    </dl>
  <//>`;
}

const COUNTERS_SHOWN = 9;

function HealthCard({ bid, info }) {
  const b = boardState(bid);
  const [all, setAll] = useState(false);
  const h = info.health || {};
  const failed = b.infoError && b.infoError.errName !== "ABSENT";
  const health = healthOf(info);
  // Non-zero counters first (they are the ones that say something), then by name.
  const counters = Object.entries(h.counters || {}).sort(([a, x], [c, y]) =>
    (Number(y) !== 0) - (Number(x) !== 0) || a.localeCompare(c));
  const shown = all ? counters : counters.slice(0, COUNTERS_SHOWN);
  return html`<${Card} title="Health" icon="activity" testid="health-card">
    ${failed ? html`<div class="mb-12"><${Reason} level="err" testid="health-failed"
      text=${`The latest read failed (${b.infoError.errName}); below is the last good one${b.infoOkAt ? `, from ${clock(b.infoOkAt)}` : ""}.`} /></div>` : null}
    <div class=${`row ${failed ? "stale" : ""}`}>
      <${Chip} level=${health.level} icon=${health.level === "ok" ? "circle-check" : "circle-alert"}>${health.text}<//>
      <span class="secondary">control channel <span class="mono">${h.control_channel || "unknown"}</span>${" · "}${h.reachable ? "reachable" : "not reachable"}</span>
    </div>
    ${(h.notes || []).map((n) => html`<div class="mt-8" key=${n} data-testid="health-note"><${Reason} text=${n}
      level=${health.level === "ok" ? "" : health.level} /></div>`)}
    ${counters.length ? html`<div class=${`counters mt-14 ${failed ? "stale" : ""}`} data-testid="counters">
      ${shown.map(([k, v]) => html`<div class="counter" key=${k}>
        <div class="counter-name" title=${k}>${k}</div><div class="counter-value">${Number(v).toLocaleString("en-GB")}</div></div>`)}
    </div>
    ${counters.length > COUNTERS_SHOWN ? html`<button type="button" class="btn ghost sm mt-8" onClick=${() => setAll(!all)}>
      ${all ? "Show fewer counters" : `Show all ${counters.length} counters`}</button>` : null}`
      : html`<p class="muted mt-12">The harness reports no counters.</p>`}
  <//>`;
}

function TelemetryCard({ bid }) {
  const b = boardState(bid);
  const readings = b.telemetry;
  const action = html`<button type="button" class="btn ghost sm" onClick=${() => loadTelemetry(bid)}
    aria-busy=${b.telemetryLoading ? "true" : undefined}>
    ${b.telemetryLoading ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`} Read again</button>`;
  return html`<${Card} title="Telemetry" icon="thermometer" actions=${action} bodyCls="flush"
      sub="Every value names its source. A value the board cannot give is shown as unavailable, never as zero."
      testid="telemetry-card">
    ${b.telemetryError ? html`<div class="pad-x"><${Reason} level="err"
        text=${`${b.telemetryError.errName}: ${b.telemetryError.message}${b.telemetry ? " (showing the last good read)" : ""}`} /></div>` : null}
    ${readings && readings.length ? html`<table class="table" data-testid="telemetry-table">
      <colgroup><col style="width:38%" /><col style="width:20%" /><col style="width:24%" /><col style="width:18%" /></colgroup>
      <thead><tr><th>Reading</th><th class="r">Value</th><th>Source</th><th>Age</th></tr></thead>
      <tbody>${readings.map((r) => {
        const ok = r.value !== null && r.value !== undefined;
        return html`<tr key=${r.name} data-reading=${r.name} data-available=${ok ? "yes" : "no"}>
          <td><span class="mono">${r.name}</span>
            ${r.reason ? html`<${Reason} text=${r.reason} level=${ok ? "" : "unk"} icon=${ok ? "info" : "circle-slash"} />` : null}</td>
          <td class="r num nowrap">${ok ? valueText(r) : html`<span class="muted">unavailable</span>`}</td>
          <td><span class="mono secondary">${r.source || "none given"}</span></td>
          <td class="muted nowrap">${ok && r.observed_at ? ageText(r.observed_at) : "-"}</td>
        </tr>`;
      })}</tbody></table>`
      : html`<p class="muted pad-x">${b.telemetryLoading ? "Reading..." : readings ? "No readings." : "Not read yet."}</p>`}
    ${b.telemetryLine ? html`<p class="muted mono pad-x pad-y small">${b.telemetryLine}</p>` : null}
  <//>`;
}

function CapabilitiesCard({ info }) {
  const names = new Set([...(info.capabilities || []), ...Object.keys(info.unavailable || {})]);
  const order = [...CAPABILITY_ORDER.filter((n) => names.has(n)), ...[...names].filter((n) => !CAPABILITY_ORDER.includes(n)).sort()];
  const available = order.filter((n) => (info.capabilities || []).includes(n));
  const missing = order.filter((n) => !(info.capabilities || []).includes(n));
  return html`<${Card} title="Capabilities" icon="list-checks" testid="capabilities-card"
      sub="What this board can do over the links it has now. A missing one says what it needs.">
    <p class="sub-head">Available (${available.length})</p>
    <div class="caps-available">${available.map((n) => html`<span class="cap" key=${n} title=${n}
      data-capability=${n}><${Icon} name="check" cls="sm i-ok" />${capTitle(n)}</span>`)}</div>
    ${missing.length ? html`<p class="sub-head mt-18">Not available (${missing.length})</p>
    <ul class="caps-missing">${missing.map((n) => html`<li key=${n} data-capability=${n}>
      <div class="cap-title"><${Icon} name="circle-slash" cls="sm i-muted" />${capTitle(n)}</div>
      <${Reason} text=${(info.unavailable || {})[n] || "not offered by this board pack"} />
    </li>`)}</ul>` : null}
  <//>`;
}

// SYSMON over JTAG takes ~3 s a read (docs/CONTRACTS.md), so telemetry is polled in the
// background, never awaited by anything else, and the last good values stay on screen.
const TELEMETRY_POLL_MS = 15000;

export function OverviewSection({ bid }) {
  const b = boardState(bid);
  useEffect(() => {
    const timer = setInterval(() => {
      const now = boardState(bid);
      if (document.visibilityState === "visible" && now.info && !now.job && !now.telemetryLoading) {
        loadTelemetry(bid);
      }
    }, TELEMETRY_POLL_MS);
    return () => clearInterval(timer);
  }, [bid]);
  if (!b.info) {
    return html`<div class="card" data-testid="info-error"><div class="card-body">
      ${b.infoError ? html`<${Reason} level="err" text=${`${b.infoLine}: ${b.infoError.errName}: ${b.infoError.message}`} />
          ${b.infoError.hint ? html`<div class="mt-8"><${Reason} text=${b.infoError.hint} /></div>` : null}
          <p class="mt-14"><button type="button" class="btn sm" onClick=${() => refreshInfo(bid)}>
            <${Icon} name="refresh-cw" /> Read again</button></p>`
        : html`<p class="muted"><${Spinner} /> Reading the board...</p>`}</div></div>`;
  }
  return html`<div class="grid split">
    <div class="stack">
      <${IdentityCard} bid=${bid} info=${b.info} />
      <${TelemetryCard} bid=${bid} />
    </div>
    <div class="stack">
      <${HealthCard} bid=${bid} info=${b.info} />
      <${CapabilitiesCard} info=${b.info} />
    </div>
  </div>`;
}
