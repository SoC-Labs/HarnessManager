// Program: overlays for this shell, the preflight, Program / Restore baseline, progress.
//
// Program is refused while any preflight row is MISMATCH. That mirrors the deploy
// service's own rule (core.pack.preflight_refusal); the service still enforces it.

import { panelState, runJob } from "../actions.js";
import { bytesText, capState, checkIcon, checkLevel, kib } from "../format.js";
import { html, useEffect } from "../lib.js";
import { boardState, changed, loadOverlays, runPreflight } from "../store.js";
import { ActionRow, ArmBox, Card, Chip, Icon, Reason, ResultBlock, Spinner } from "../ui.js";

const ARM = "program";
const PHASES = ["guard", "swap", "push", "verify"];

function rows(b) {
  const o = b.overlays;
  if (!o) return [];
  const out = o.loadable.map((ov) => ({ name: ov.name, ov, blocked: "" }));
  for (const [name, reason] of Object.entries(o.blocked || {})) {
    const full = (o.all || []).find((x) => x.name === name) || null;
    out.push({ name, ov: full, blocked: reason });
  }
  return out;
}

function pick(bid, name) {
  const b = boardState(bid);
  b.selectedOverlay = name;
  changed();
  runPreflight(bid, name);
}

// What to send as `overlay`: the OverlayRef itself when the list gave one (exact even when
// two overlays share a name across shells), else the name (socharnessd resolves both).
function overlaySpec(b, name) {
  const o = b.overlays;
  const ref = o && ((o.loadable || []).find((x) => x.name === name) || (o.all || []).find((x) => x.name === name));
  return ref || name;
}

function OverlaysCard({ bid }) {
  const b = boardState(bid);
  const loaded = b.info && b.info.identity && b.info.identity.rm_id;
  const shell = b.info && b.info.identity && b.info.identity.shell_id;
  const list = rows(b);
  const refresh = html`<button type="button" class="btn ghost sm" onClick=${() => loadOverlays(bid)}
    aria-busy=${b.overlaysLoading ? "true" : undefined}>
    ${b.overlaysLoading ? html`<${Spinner} />` : html`<${Icon} name="refresh-cw" />`} Refresh</button>`;
  const onKey = (e, name) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(bid, name); }
  };
  return html`<${Card} title="Overlays" icon="layers" actions=${refresh} bodyCls="flush" testid="overlays-card"
      sub=${`Designs for shell ${shell || "?"}. Select one to run its preflight.`}>
    ${b.overlaysError ? html`<div class="pad-x"><${Reason} level="err" text=${`${b.overlaysLine}: ${b.overlaysError.errName}: ${b.overlaysError.message}`} /></div>` : null}
    ${list.length ? html`<table class="table" role="radiogroup" aria-label="Overlays" data-testid="overlay-table">
      <colgroup><col style="width:36%" /><col style="width:22%" /><col style="width:14%" /><col /></colgroup>
      <thead><tr><th>Overlay</th><th>rm_id</th><th class="r">Size</th><th>Status</th></tr></thead>
      <tbody>${list.map(({ name, ov, blocked }) => {
        const selected = b.selectedOverlay === name;
        const isLoaded = ov && loaded && ov.rm_id === loaded;
        return html`<tr key=${name} class="pick" role="radio" tabindex="0" aria-checked=${selected ? "true" : "false"}
            aria-selected=${selected ? "true" : "false"} data-overlay=${name}
            onClick=${() => pick(bid, name)} onKeyDown=${(e) => onKey(e, name)}>
          <td><span class="mono">${name}</span>
            ${ov ? html`<div class="sub" title="IP class, and the shell this overlay is keyed to">${ov.ip_class} IP${
              ov.static_id !== shell ? html` · keyed to <span class="mono">${ov.static_id}</span>` : null}</div>` : null}</td>
          ${ov ? html`<td class="mono secondary">${ov.rm_id}</td>
            <td class="r num secondary nowrap">${kib(ov.size_bytes)}</td>` : null}
          <td colspan=${ov ? undefined : 3}>${blocked ? html`<${Reason} text=${blocked} icon="circle-slash" testid=${`blocked-${name}`} />`
            : isLoaded ? html`<${Chip} level="accent" icon="check">Loaded now<//>`
            : html`<span class="secondary"><${Icon} name="circle-check" cls="sm i-ok" /> Loadable</span>`}</td>
        </tr>`;
      })}</tbody></table>`
      : html`<p class="muted pad-x">${b.overlaysLoading ? "Reading the overlay list..." : "No overlays known for this board."}</p>`}
    ${b.overlaysLine ? html`<p class="muted mono pad-x small">${b.overlaysLine}${b.overlays ? `: ${b.overlays.loadable.length} loadable, ${Object.keys(b.overlays.blocked || {}).length} not` : ""}</p>` : null}
  <//>`;
}

function PreflightCard({ bid }) {
  const b = boardState(bid);
  const items = b.preflight ? b.preflight.items : [];
  const bad = items.filter((i) => i.check === "mismatch").length;
  const unchecked = items.filter((i) => i.check === "unchecked").length;
  let summary = null;
  if (b.preflight) {
    const level = bad ? "err" : unchecked ? "unk" : "ok";
    const text = bad ? `${bad} mismatch${bad > 1 ? "es" : ""}: Program is refused`
      : `no mismatch${unchecked ? `, ${unchecked} unchecked (not a pass, does not block)` : ""}`;
    summary = html`<${Reason} level=${level} text=${text} testid="preflight-summary" />`;
  }
  return html`<${Card} title="Preflight" icon="shield-check" bodyCls="flush" testid="preflight-card"
      sub="Every check must pass. Unchecked means it could not be compared; it is not a pass.">
    ${!b.selectedOverlay ? html`<p class="muted pad-x">Select an overlay above.</p>` : null}
    ${b.preflightLoading ? html`<p class="muted pad-x"><${Spinner} /> Running the preflight for ${b.preflightFor}...</p>` : null}
    ${b.preflightError ? html`<div class="pad-x"><${Reason} level="err" text=${`${b.preflightError.errName}: ${b.preflightError.message}`} /></div>` : null}
    ${items.length ? html`<ul class="checks" data-testid="preflight-list">${items.map((i) => html`
      <li key=${i.name} data-check=${i.check}>
        <${Icon} name=${checkIcon(i.check)} cls=${`i-${checkLevel(i.check)}`} />
        <div><div class="name">${i.name}</div><div class="detail">${i.detail}</div></div>
        <${Chip} level=${checkLevel(i.check)}>${i.check.toUpperCase()}<//>
      </li>`)}</ul>` : null}
    ${summary || b.preflightLine ? html`<div class="pad-x pad-y">${summary}
      <p class="muted mono small">${b.preflightLine}</p></div>` : null}
  <//>`;
}

function programGuard(bid) {
  const b = boardState(bid);
  if (!b.selectedOverlay) return "select an overlay first";
  if (b.preflightLoading || b.preflightFor !== b.selectedOverlay || !b.preflight) {
    return b.preflightError ? `the preflight of ${b.selectedOverlay} failed to run`
      : `waiting for the preflight of ${b.selectedOverlay}`;
  }
  const bad = b.preflight.items.filter((i) => i.check === "mismatch").map((i) => i.name);
  if (bad.length) return `preflight MISMATCH (${bad.join(", ")}): Program is refused`;
  return "";
}

function renderDeploy(result) {
  if (!result || typeof result !== "object") return [{ kind: "out", text: String(result) }];
  const verdict = result.verified ? "verified by the board" : "WRITTEN, NOT VERIFIED";
  return [
    { kind: result.verified ? "ok" : "warnline", text: `rm_id ${result.rm_id}: ${verdict}` },
    { kind: "out", text: `transport ${result.transport || "?"}, ${Number(result.seconds || 0).toFixed(1)} s` },
  ];
}

function ProgramCard({ bid }) {
  const b = boardState(bid);
  const p = panelState(bid, "program");
  const name = b.selectedOverlay;
  const progress = (ctx) => (d) => ctx.progress(
    d.total > 1 ? `${d.phase}: ${bytesText(d.done)} of ${bytesText(d.total)}` : `${d.phase || "?"}: done`,
    d.phase || "?");
  const program = {
    key: "program", label: "Program", busyLabel: "Programming...", budgetS: 120,
    command: `program ${name || "?"}`,
    run: (ctx) => runJob("deploy", { bid }, { overlay: overlaySpec(b, name) }, progress(ctx), "deploy"),
    render: renderDeploy,
    onDone: (ok, value) => {
      // socharnessd runs the preflight before it takes the job, and a refusal carries the
      // items it refused on: show them where the preflight lives.
      const items = !ok && value && value.data && value.data.preflight;
      if (Array.isArray(items)) {
        b.preflight = { items, refusal: null };
        b.preflightFor = name;
        b.preflightLine = `$ preflight ${name}  (from the refused deploy)`;
        changed();
      }
    },
  };
  const restore = {
    key: "restore", label: "Restore baseline", busyLabel: "Restoring...", budgetS: 120,
    command: "restore",
    run: (ctx) => runJob("restore", { bid }, undefined, progress(ctx), "restore"),
    render: renderDeploy,
  };
  return html`<${Card} title="Program the partition" icon="upload" testid="program-card"
      sub="Pushes the overlay once its preflight passes, then re-reads the board to confirm it.">
    <div class="actions">
      <div class="field"><label>Selected</label>
        ${name ? html`<span class="mono" data-testid="selected-overlay">${name}</span>` : html`<span class="muted">none</span>`}</div>
      <${ArmBox} bid=${bid} armKey=${ARM} testid="arm-program"
        text="Arm: I understand this reconfigures the partition and resets the DUT." />
      <${ActionRow} bid=${bid} panel="program" spec=${program} variant="primary" icon="upload"
        gate=${{ capability: "deploy_partial", arm: ARM, guard: () => programGuard(bid) }} />
      <${ActionRow} bid=${bid} panel="program" spec=${restore} icon="undo-2" quietArm=${true}
        gate=${{ capability: "deploy_partial", arm: ARM }} />
      <${ResultBlock} lines=${p.lines} panel=${p} testid="program-result"
        placeholder="Restore baseline loads the board's safe design (the greybox on the MPS3)." />
    </div>
  <//>`;
}

function ProgressCard({ bid }) {
  const b = boardState(bid);
  const d = b.deploy;
  const seen = d.phases;
  const current = d.state === "running" ? d.phase : "";
  const pct = d.state === "done" ? 100 : d.total ? Math.min(100, Math.round((d.bytes * 100) / d.total)) : 0;
  const stepState = (ph) => {
    if (d.state === "idle") return "";
    if (d.state === "failed" && (ph === d.phase || (!seen.includes(ph) && ph === PHASES[seen.length]))) return "failed";
    if (d.state === "done") return "done";
    if (ph === current) return "active";
    if (seen.includes(ph)) return "done";
    return "";
  };
  let outcome = null;
  if (d.state === "done") {
    outcome = d.verified
      ? html`<div class="outcome ok" data-testid="deploy-outcome" data-state="done"><${Icon} name="circle-check" />
          <div><strong>Done.</strong> rm_id <span class="mono">${d.rm_id}</span> is verified by the board${d.seconds ? ` in ${d.seconds.toFixed(1)} s` : ""}${d.transport ? ` over ${d.transport}` : ""}.</div></div>`
      : html`<div class="outcome warn" data-testid="deploy-outcome" data-state="unverified"><${Icon} name="triangle-alert" />
          <div><strong>Written, not verified.</strong> rm_id <span class="mono">${d.rm_id}</span>.</div></div>`;
  } else if (d.state === "failed") {
    outcome = html`<div class="outcome err" data-testid="deploy-outcome" data-state="failed"><${Icon} name="circle-x" />
      <div><strong>Failed${d.stage ? ` at ${d.stage}` : ""}.</strong> ${d.reason}</div></div>`;
  }
  const phaseText = d.state === "idle" ? "Idle: nothing is being programmed."
    : d.state === "running" ? `${d.phase || "starting"}${d.total > 1 ? `: ${bytesText(d.bytes)} of ${bytesText(d.total)}` : ""}`
    : d.state === "done" ? "Complete" : "Stopped";
  return html`<${Card} title="Progress" icon="activity" testid="progress-card"
      sub=${d.overlay ? `Deploy events for ${d.overlay}, in the order the engine sent them.` : "Deploy events, in the order the engine sent them."}>
    <div class="steps" aria-hidden="true">${PHASES.map((ph) => html`<div key=${ph} class=${`step ${stepState(ph)}`}>
      <div class="bar"></div><span>${ph}</span></div>`)}</div>
    <div class=${`meter ${d.state === "done" ? "done" : d.state === "failed" ? "failed" : ""}`}
      role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${pct} aria-label="Deploy progress">
      <div style=${`width:${pct}%`}></div></div>
    <div class="meter-line"><span data-testid="deploy-phase">${phaseText}</span><span class="num">${d.state !== "idle" ? `${pct}%` : ""}</span></div>
    ${outcome}
    ${d.events.length ? html`<ul class="event-list" data-testid="deploy-events">${d.events.slice(-40).map((e, i) => html`<li key=${i}>${e}</li>`)}</ul>` : null}
  <//>`;
}

export function ProgramSection({ bid }) {
  const b = boardState(bid);
  useEffect(() => {
    if (!b.overlays && !b.overlaysLoading) loadOverlays(bid);
  }, [bid]);
  const cap = capState(b.info, "deploy_partial");
  return html`<div class="stack">
    ${cap && !cap.available ? html`<${Reason} text=${`Programming is not available on this board: ${cap.reason}`} icon="circle-slash" />` : null}
    <div class="grid program">
      <div class="stack">
        <${OverlaysCard} bid=${bid} />
        <${PreflightCard} bid=${bid} />
      </div>
      <div class="stack">
        <${ProgramCard} bid=${bid} />
        <${ProgressCard} bid=${bid} />
      </div>
    </div>
  </div>`;
}
